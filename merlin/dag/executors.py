#
# Copyright (c) 2022, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import logging

import dask
import pandas as pd
from dask.core import flatten

from merlin.core.dispatch import concat_columns, is_list_dtype, list_val_dtype
from merlin.core.utils import (
    ensure_optimize_dataframe_graph,
    global_dask_client,
    set_client_deprecated,
)
from merlin.dag import ColumnSelector, Graph, Node
from merlin.io.worker import clean_worker_cache

LOG = logging.getLogger("merlin")


class LocalExecutor:
    """
    An executor for running Merlin operator DAGs locally
    """

    def transform(
        self,
        transformable,
        graph,
        output_dtypes=None,
        additional_columns=None,
        capture_dtypes=False,
    ):
        """
        Transforms a single dataframe (possibly a partition of a Dask Dataframe)
        by applying the operators from a collection of Nodes
        """
        nodes = []
        if isinstance(graph, Graph):
            nodes.append(graph.output_node)
        elif isinstance(graph, Node):
            nodes.append(graph)
        elif isinstance(graph, list):
            nodes = graph
        else:
            raise TypeError(
                f"LocalExecutor detected unsupported type of input for graph: {type(graph)}."
                " `graph` argument must be either a `Graph` object (preferred)"
                " or a list of `Node` objects (deprecated, but supported for backward "
                " compatibility.)"
            )

        output_data = None

        for node in nodes:
            input_data = self._build_input_data(node, transformable, capture_dtypes=capture_dtypes)

            if node.op:
                transformed_data = self._transform_data(
                    node, input_data, capture_dtypes=capture_dtypes
                )
            else:
                transformed_data = input_data

            output_data = self._combine_node_outputs(node, transformed_data, output_data)

        if additional_columns:
            output_data = concat_columns(
                [output_data, transformable[_get_unique(additional_columns)]]
            )

        return output_data

    def _build_input_data(self, node, transformable, capture_dtypes=False):
        """
        Recurse through the graph executing parent and dependency operators
        to form the input dataframe for each output node
        Parameters
        ----------
        node : Node
            Output node of the graph to execute
        transformable : Transformable
            Dataframe to run the graph ending with node on
        capture_dtypes : bool, optional
            Overrides the schema dtypes with the actual dtypes when True, by default False
        Returns
        -------
        Transformable
            The input DataFrame or DictArray formed from
            the outputs of upstream parent/dependency nodes
        """
        node_input_cols = _get_unique(node.input_schema.column_names)
        addl_input_cols = set(node.dependency_columns.names)

        if node.parents_with_dependencies:
            # If there are parents, collect their outputs
            # to build the current node's input
            input_data = None
            seen_columns = None

            for parent in node.parents_with_dependencies:
                parent_output_cols = _get_unique(parent.output_schema.column_names)
                parent_data = self.transform(transformable, [parent], capture_dtypes=capture_dtypes)
                if input_data is None or not len(input_data):
                    input_data = parent_data[parent_output_cols]
                    seen_columns = set(parent_output_cols)
                else:
                    new_columns = set(parent_output_cols) - seen_columns
                    input_data = concat_columns([input_data, parent_data[list(new_columns)]])
                    seen_columns.update(new_columns)

            # Check for additional input columns that aren't generated by parents
            # and fetch them from the root DataFrame or DictArray
            unseen_columns = set(node.input_schema.column_names) - seen_columns
            addl_input_cols = addl_input_cols.union(unseen_columns)

            # TODO: Find a better way to remove dupes
            addl_input_cols = addl_input_cols - set(input_data.columns)

            if addl_input_cols:
                input_data = concat_columns([input_data, transformable[list(addl_input_cols)]])
        else:
            # If there are no parents, this is an input node,
            # so pull columns directly from root data
            addl_input_cols = list(addl_input_cols) if addl_input_cols else []
            input_data = transformable[node_input_cols + addl_input_cols]

        return input_data

    def _transform_data(self, node, input_data, capture_dtypes=False):
        """
        Run the transform represented by the final node in the graph
        and check output dtypes against the output schema
        Parameters
        ----------
        node : Node
            Output node of the graph to execute
        input_data : Transformable
            Dataframe to run the graph ending with node on
        capture_dtypes : bool, optional
            Overrides the schema dtypes with the actual dtypes when True, by default False
        Returns
        -------
        Transformable
            The output DataFrame or DictArray formed by executing the final node's transform
        Raises
        ------
        TypeError
            If the transformed output columns don't have the same dtypes
            as the output schema columns
        RuntimeError
            If no DataFrame or DictArray is returned from the operator
        """
        try:
            # use input_columns to ensure correct grouping (subgroups)
            selection = node.input_columns.resolve(node.input_schema)
            output_data = node.op.transform(selection, input_data)

            # Update or validate output_data dtypes
            for col_name, output_col_schema in node.output_schema.column_schemas.items():
                col_series = output_data[col_name]
                col_dtype = col_series.dtype
                is_list = is_list_dtype(col_series)

                if is_list:
                    col_dtype = list_val_dtype(col_series)
                if hasattr(col_dtype, "as_numpy_dtype"):
                    col_dtype = col_dtype.as_numpy_dtype()
                elif hasattr(col_series, "numpy"):
                    col_dtype = col_series[0].cpu().numpy().dtype

                output_data_schema = output_col_schema.with_dtype(col_dtype, is_list=is_list)

                if capture_dtypes:
                    node.output_schema.column_schemas[col_name] = output_data_schema
                elif len(output_data):
                    if output_col_schema.dtype != output_data_schema.dtype:
                        raise TypeError(
                            f"Dtype discrepancy detected for column {col_name}: "
                            f"operator {node.op.label} reported dtype "
                            f"`{output_col_schema.dtype}` but returned dtype "
                            f"`{output_data_schema.dtype}`."
                        )
        except Exception:
            LOG.exception("Failed to transform operator %s", node.op)
            raise
        if output_data is None:
            raise RuntimeError(f"Operator {node.op} didn't return a value during transform")

        return output_data

    def _combine_node_outputs(self, node, transformed_data, output):
        node_output_cols = _get_unique(node.output_schema.column_names)

        # dask needs output to be in the same order defined as meta, reorder partitions here
        # this also selects columns (handling the case of removing columns from the output using
        # "-" overload)
        if output is None:
            output = transformed_data[node_output_cols]
        else:
            output = concat_columns([output, transformed_data[node_output_cols]])

        return output


class DaskExecutor:
    """
    An executor for running Merlin operator DAGs as distributed Dask jobs
    """

    def __init__(self, client=None):
        self._executor = LocalExecutor()

        # Deprecate `client`
        if client is not None:
            set_client_deprecated(client, "DaskExecutor")

    def __getstate__(self):
        # dask client objects aren't picklable - exclude from saved representation
        return {k: v for k, v in self.__dict__.items() if k != "client"}

    def transform(
        self, ddf, graph, output_dtypes=None, additional_columns=None, capture_dtypes=False
    ):
        """
        Transforms all partitions of a Dask Dataframe by applying the operators
        from a collection of Nodes
        """
        nodes = []
        if isinstance(graph, Graph):
            nodes.append(graph.output_node)
        elif isinstance(graph, Node):
            nodes.append(graph)
        elif isinstance(graph, list):
            nodes = graph
        else:
            raise TypeError(
                f"DaskExecutor detected unsupported type of input for graph: {type(graph)}."
                " `graph` argument must be either a `Graph` object (preferred)"
                " or a list of `Node` objects (deprecated, but supported for backward"
                " compatibility.)"
            )

        self._clear_worker_cache()

        # Check if we are only selecting columns (no transforms).
        # If so, we should perform column selection at the ddf level.
        # Otherwise, Dask will not push the column selection into the
        # IO function.
        if not nodes:
            return ddf[_get_unique(additional_columns)] if additional_columns else ddf

        if isinstance(nodes, Node):
            nodes = [nodes]

        columns = list(flatten(wfn.output_columns.names for wfn in nodes))
        columns += additional_columns if additional_columns else []

        if isinstance(output_dtypes, dict) and isinstance(ddf._meta, pd.DataFrame):
            dtypes = output_dtypes
            output_dtypes = type(ddf._meta)({k: [] for k in columns})
            for column, dtype in dtypes.items():
                output_dtypes[column] = output_dtypes[column].astype(dtype)

        elif not output_dtypes:
            # TODO: constructing meta like this loses dtype information on the ddf
            # and sets it all to 'float64'. We should propagate dtype information along
            # with column names in the columngroup graph. This currently only
            # happens during intermediate 'fit' transforms, so as long as statoperators
            # don't require dtype information on the DDF this doesn't matter all that much
            output_dtypes = type(ddf._meta)({k: [] for k in columns})

        return ensure_optimize_dataframe_graph(
            ddf=ddf.map_partitions(
                self._executor.transform,
                nodes,
                additional_columns=additional_columns,
                capture_dtypes=capture_dtypes,
                meta=output_dtypes,
                enforce_metadata=False,
            )
        )

    def fit(self, ddf, nodes):
        """Calculates statistics for a set of nodes on the input dataframe

        Parameters
        -----------
        ddf: dask.Dataframe
            The input dataframe to calculate statistics for. If there is a
            train/test split this should be the training dataset only.
        """
        stats = []
        for node in nodes:
            # Check for additional input columns that aren't generated by parents
            addl_input_cols = set()
            if node.parents:
                upstream_output_cols = sum(
                    [upstream.output_columns for upstream in node.parents_with_dependencies],
                    ColumnSelector(),
                )
                addl_input_cols = set(node.input_columns.names) - set(upstream_output_cols.names)

            # apply transforms necessary for the inputs to the current column group, ignoring
            # the transforms from the statop itself
            transformed_ddf = self.transform(
                ddf,
                node.parents_with_dependencies,
                additional_columns=addl_input_cols,
                capture_dtypes=True,
            )

            try:
                stats.append(node.op.fit(node.input_columns, transformed_ddf))
            except Exception:
                LOG.exception("Failed to fit operator %s", node.op)
                raise

        dask_client = global_dask_client()
        if dask_client:
            results = [r.result() for r in dask_client.compute(stats)]
        else:
            results = dask.compute(stats, scheduler="synchronous")[0]

        for computed_stats, node in zip(results, nodes):
            node.op.fit_finalize(computed_stats)

    def _clear_worker_cache(self):
        # Clear worker caches to be "safe"
        dask_client = global_dask_client()
        if dask_client:
            dask_client.run(clean_worker_cache)
        else:
            clean_worker_cache()


def _get_unique(cols):
    # Need to preserve order in unique-column list
    return list({x: x for x in cols}.keys())
