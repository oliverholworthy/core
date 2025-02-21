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
from __future__ import annotations

from enum import Flag, auto
from typing import Any, List, Optional, Union

import merlin.dag
from merlin.core.protocols import Transformable
from merlin.dag.selector import ColumnSelector
from merlin.schema import ColumnSchema, Schema


class Supports(Flag):
    """Indicates what type of data representation this operator supports for transformations"""

    # cudf dataframe
    CPU_DATAFRAME = auto()
    # pandas dataframe
    GPU_DATAFRAME = auto()
    # dict of column name to numpy array
    CPU_DICT_ARRAY = auto()
    # dict of column name to cupy array
    GPU_DICT_ARRAY = auto()


class BaseOperator:
    """
    Base class for all operator classes.
    """

    def compute_selector(
        self,
        input_schema: Schema,
        selector: ColumnSelector,
        parents_selector: Optional[ColumnSelector] = None,
        dependencies_selector: Optional[ColumnSelector] = None,
    ) -> ColumnSelector:
        """
        Provides a hook method for sub-classes to override to implement
        custom column selection logic.

        Parameters
        ----------
        input_schema : Schema
            Schemas of the columns to apply this operator to
        selector : ColumnSelector
            Column selector to apply to the input schema
        parents_selector : ColumnSelector
            Combined selectors of the upstream parents feeding into this operator
        dependencies_selector : ColumnSelector
            Combined selectors of the upstream dependencies feeding into this operator

        Returns
        -------
        ColumnSelector
            Revised column selector to apply to the input schema
        """
        selector = selector or ColumnSelector("*")

        self._validate_matching_cols(input_schema, selector, self.compute_selector.__name__)

        return selector.resolve(input_schema)

    def compute_input_schema(
        self,
        root_schema: Schema,
        parents_schema: Schema,
        deps_schema: Schema,
        selector: ColumnSelector,
    ) -> Schema:
        """Given the schemas coming from upstream sources and a column selector for the
        input columns, returns a set of schemas for the input columns this operator will use

        Parameters
        -----------
        root_schema: Schema
            Base schema of the dataset before running any operators.
        parents_schema: Schema
            The combined schemas of the upstream parents feeding into this operator
        deps_schema: Schema
            The combined schemas of the upstream dependencies feeding into this operator
        col_selector: ColumnSelector
            The column selector to apply to the input schema

        Returns
        -------
        Schema
            The schemas of the columns used by this operator
        """
        self._validate_matching_cols(
            parents_schema + deps_schema, selector, self.compute_input_schema.__name__
        )

        return parents_schema + deps_schema

    def compute_output_schema(
        self,
        input_schema: Schema,
        col_selector: ColumnSelector,
        prev_output_schema: Optional[Schema] = None,
    ) -> Schema:
        """
        Given a set of schemas and a column selector for the input columns,
        returns a set of schemas for the transformed columns this operator will produce

        Parameters
        -----------
        input_schema: Schema
            The schemas of the columns to apply this operator to
        col_selector: ColumnSelector
            The column selector to apply to the input schema

        Returns
        -------
        Schema
            The schemas of the columns produced by this operator
        """
        if not col_selector:
            col_selector = ColumnSelector(input_schema.column_names)

        if col_selector.tags:
            tags_col_selector = ColumnSelector(tags=col_selector.tags)
            filtered_schema = input_schema.apply(tags_col_selector)
            col_selector += ColumnSelector(filtered_schema.column_names)

            # zero tags because already filtered
            col_selector._tags = []

        self._validate_matching_cols(
            input_schema, col_selector, self.compute_output_schema.__name__
        )

        output_schema = Schema()
        for output_col_name, input_col_names in self.column_mapping(col_selector).items():
            col_schema = ColumnSchema(output_col_name)
            col_schema = self._compute_dtype(col_schema, input_schema[input_col_names])
            col_schema = self._compute_tags(col_schema, input_schema[input_col_names])
            col_schema = self._compute_properties(col_schema, input_schema[input_col_names])
            output_schema += Schema([col_schema])

        if self.dynamic_dtypes and prev_output_schema:
            for col_name, col_schema in output_schema.column_schemas.items():
                dtype = prev_output_schema[col_name].dtype
                output_schema.column_schemas[col_name] = col_schema.with_dtype(dtype)

        return output_schema

    def validate_schemas(
        self,
        parents_schema: Schema,
        deps_schema: Schema,
        input_schema: Schema,
        output_schema: Schema,
        strict_dtypes: bool = False,
    ):
        """
        Provides a hook method that sub-classes can override to implement schema validation logic.

        Sub-class implementations should raise an exception if the schemas are not valid for the
        operations they implement.

        Parameters
        ----------
        parents_schema : Schema
            The combined schemas of the upstream parents feeding into this operator
        deps_schema : Schema
            The combined schemas of the upstream dependencies feeding into this operator
        input_schema : Schema
            The schemas of the columns to apply this operator to
        output_schema : Schema
            The schemas of the columns produced by this operator
        strict_dtypes : Boolean, optional
            Enables strict checking for column dtype matching if True, by default False
        """

    def transform(
        self, col_selector: ColumnSelector, transformable: Transformable
    ) -> Transformable:
        """Transform the dataframe by applying this operator to the set of input columns

        Parameters
        -----------
        col_selector: ColumnSelector
            The columns to apply this operator to
        transformable: Transformable
            A pandas or cudf dataframe that this operator will work on

        Returns
        -------
        Transformable
            Returns a transformed dataframe or dictarray for this operator
        """
        return transformable

    def column_mapping(self, col_selector):
        """
        Compute which output columns depend on which input columns

        Parameters
        ----------
        col_selector : ColumnSelector
            A selector containing a list of column names

        Returns
        -------
        Dict[str, List[str]]
            Mapping from output column names to list of the input columns they rely on
        """
        column_mapping = {}
        for col_name in col_selector.names:
            column_mapping[col_name] = [col_name]
        return column_mapping

    def compute_column_schema(self, col_name, input_schema):
        methods = [self._compute_dtype, self._compute_tags, self._compute_properties]
        return self._compute_column_schema(col_name, input_schema, methods=methods)

    def _compute_column_schema(self, col_name, input_schema, methods=None):
        col_schema = ColumnSchema(col_name)

        for method in methods:
            col_schema = method(col_schema, input_schema)

        return col_schema

    def _compute_dtype(self, col_schema, input_schema):
        dtype = col_schema.dtype
        is_list = col_schema.is_list
        is_ragged = col_schema.is_ragged

        if input_schema.column_schemas:
            source_col_name = input_schema.column_names[0]
            dtype = input_schema[source_col_name].dtype
            is_list = input_schema[source_col_name].is_list
            is_ragged = input_schema[source_col_name].is_ragged

        if self.output_dtype is not None:
            dtype = self.output_dtype
            is_list = any(cs.is_list for _, cs in input_schema.column_schemas.items())
            is_ragged = any(cs.is_ragged for _, cs in input_schema.column_schemas.items())

        return col_schema.with_dtype(dtype, is_list=is_list, is_ragged=is_ragged)

    @property
    def dynamic_dtypes(self):
        return False

    def _compute_tags(self, col_schema, input_schema):
        tags = []
        if input_schema.column_schemas:
            source_col_name = input_schema.column_names[0]
            tags = input_schema[source_col_name].tags

        # Override empty tags with tags from the input schema
        # Override input schema tags with the output tags of this operator
        return col_schema.with_tags(tags).with_tags(self.output_tags)

    def _compute_properties(self, col_schema, input_schema):
        properties = {}

        if input_schema.column_schemas:
            source_col_name = input_schema.column_names[0]
            properties.update(input_schema.column_schemas[source_col_name].properties)

        properties.update(self.output_properties)

        return col_schema.with_properties(properties)

    def _validate_matching_cols(self, schema, selector, method_name):
        selector = selector or ColumnSelector()
        resolved_selector = selector.resolve(schema)

        missing_cols = [name for name in selector.names if name not in resolved_selector.names]
        if missing_cols:
            raise ValueError(
                f"Missing columns {missing_cols} found in operator"
                f"{self.__class__.__name__} during {method_name}."
            )

    # TODO: Update instructions for how to define custom
    # operators to reflect constructing the column mapping
    # (They should no longer override this method)
    def output_column_names(self, col_selector: ColumnSelector) -> ColumnSelector:
        """Given a set of columns names returns the names of the transformed columns this
        operator will produce

        Parameters
        -----------
        columns: list of str, or list of list of str
            The columns to apply this operator to

        Returns
        -------
        list of str, or list of list of str
            The names of columns produced by this operator
        """
        return ColumnSelector(list(self.column_mapping(col_selector).keys()))

    @property
    def dependencies(self) -> List[Union[str, Any]]:
        """Defines an optional list of column dependencies for this operator.
        This lets you consume columns that aren't part of the main transformation workflow.

        Returns
        -------
        str, list of str or ColumnSelector, optional
            Extra dependencies of this operator. Defaults to None
        """
        return []

    def __rrshift__(self, other):
        return ColumnSelector(other) >> self

    @property
    def output_dtype(self):
        return None

    @property
    def output_tags(self):
        return []

    @property
    def output_properties(self):
        return {}

    @property
    def label(self) -> str:
        return self.__class__.__name__

    def create_node(self, selector):
        return merlin.dag.Node(selector)

    @property
    def supports(self) -> Supports:
        """Returns what kind of data representation this operator supports"""
        return Supports.CPU_DATAFRAME | Supports.GPU_DATAFRAME

    def _get_columns(self, df, selector):
        if isinstance(df, dict):
            return {col_name: df[col_name] for col_name in selector.names}
        else:
            return df[selector.names]
