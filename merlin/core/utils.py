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
import gzip
import importlib
import os
import shutil
import tarfile
import urllib.request
import warnings
import zipfile
from contextvars import ContextVar
from typing import Any, Callable, Optional

import dask
import distributed
from dask.dataframe.optimize import optimize as dd_optimize
from dask.distributed import Client, get_client
from tqdm import tqdm

from merlin.core.compat import HAS_GPU, cuda

_merlin_dask_client = ContextVar("_merlin_dask_client", default="auto")


try:
    import psutil
except ImportError:
    psutil = None


def pynvml_mem_size(kind="total", index=0):
    """Get Memory Info for device.

    Parameters
    ----------
    kind : str, optional
        Either "free" or "total", by default "total"
    index : int, optional
        Device Index, by default 0

    Returns
    -------
    int
        Either free or total memory on device depending on the kind parameter.

    Raises
    ------
    ValueError
        When kind is not one of {"free", "total"}
    """
    import pynvml

    pynvml.nvmlInit()
    size = None
    if kind == "free":
        size = int(pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(index)).free)
    elif kind == "total":
        size = int(pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(index)).total)
    else:
        raise ValueError(f"{kind} not a supported option for device_mem_size.")
    pynvml.nvmlShutdown()
    return size


def device_mem_size(kind="total", cpu=False):
    """Get Memory Info for either CPU or GPU.

    Parameters
    ----------
    kind : str, optional
        Either "total" or "free", by default "total"
    cpu : bool, optional
        Specifies whether to check memory for CPU or GPU, by default False

    Returns
    -------
    int
        Free or total memory on device

    Raises
    ------
    ValueError
        When kind is provided with an unsupported value.
    """
    # Use psutil (if available) for cpu mode
    if cpu and psutil:
        if kind == "total":
            return psutil.virtual_memory().total
        elif kind == "free":
            return psutil.virtual_memory().free
    elif cpu:
        warnings.warn("Please install psutil for full cpu=True support.")
        # Assume 1GB of memory
        return int(1e9)

    if kind not in ["free", "total"]:
        raise ValueError(f"{kind} not a supported option for device_mem_size.")
    try:
        if kind == "free":
            return int(cuda.current_context().get_memory_info()[0])
        else:
            return int(cuda.current_context().get_memory_info()[1])
    except NotImplementedError:
        if kind == "free":
            # Not using NVML "free" memory, because it will not include RMM-managed memory
            warnings.warn("get_memory_info is not supported. Using total device memory from NVML.")
        size = pynvml_mem_size(kind="total", index=0)
        return size


def get_rmm_size(size):
    return (size // 256) * 256


def download_file(url, local_filename, unzip_files=True, redownload=True):
    """utility function to download a dataset file (movielens/criteo/rossmann etc)
    locally, displaying a progress bar during download"""
    local_filename = os.path.abspath(local_filename)
    path = os.path.dirname(local_filename)
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

    if not url.startswith("http"):
        raise ValueError(f"Unhandled url scheme on {url} - this function only is for http")

    if redownload or not os.path.exists(local_filename):
        desc = f"downloading {os.path.basename(local_filename)}"
        with tqdm(unit="B", unit_scale=True, desc=desc) as progress:

            def report(chunk, chunksize, total):
                if not progress.total:
                    progress.reset(total=total)
                progress.update(chunksize)

            opener = urllib.request.build_opener()
            opener.addheaders = [("Accept-Encoding", "gzip, deflate"), ("Accept", "*/*")]

            urllib.request.install_opener(opener)
            urllib.request.urlretrieve(url, local_filename, reporthook=report)  # nosec

    if unzip_files and local_filename.endswith(".zip"):
        with zipfile.ZipFile(local_filename) as z:
            for filename in tqdm(z.infolist(), desc="unzipping files", unit="files"):
                z.extract(filename, path)

    elif unzip_files and local_filename.endswith(".tgz"):
        with tarfile.open(local_filename, "r") as tar:
            for filename in tqdm(tar.getnames(), desc="untarring files", unit="files"):
                tar.extract(filename, path)

    elif unzip_files and local_filename.endswith(".gz"):
        with gzip.open(local_filename, "rb") as input_file:
            with open(local_filename[:-3], "wb") as output_file:
                shutil.copyfileobj(input_file, output_file)


def ensure_optimize_dataframe_graph(ddf=None, dsk=None, keys=None):
    """Perform HLG DataFrame optimizations

    If `ddf` is specified, an optimized Dataframe
    collection will be returned. If `dsk` and `keys`
    are specified, an optimized graph will be returned.

    These optimizations are performed automatically
    when a DataFrame collection is computed/persisted,
    but they are NOT always performed when statistics
    are computed. The purpose of this utility is to
    ensure that the Dataframe-based optimizations are
    always applied.

    Parameters
    ----------
    ddf : dask_cudf.DataFrame, optional
        The dataframe to optimize, by default None
    dsk : dask.highlevelgraph.HighLevelGraph, optional
        Dask high level graph, by default None
    keys : List[str], optional
        The keys to optimize, by default None

    Returns
    -------
    Union[dask_cudf.DataFrame, dask.highlevelgraph.HighLevelGraph]
        A dask_cudf DataFrame or dask HighLevelGraph depending
        on the parameters provided.

    Raises
    ------
    ValueError
        If ddf is not provided and one of dsk or keys are None.
    """

    if ddf is None:
        if dsk is None or keys is None:
            raise ValueError("Must specify both `dsk` and `keys` if `ddf` is not supplied.")
    dsk = ddf.dask if dsk is None else dsk
    keys = ddf.__dask_keys__() if keys is None else keys

    if isinstance(dsk, dask.highlevelgraph.HighLevelGraph):
        with dask.config.set({"optimization.fuse.active": False}):
            dsk = dd_optimize(dsk, keys=keys)

    if ddf is None:
        # Return optimized graph
        return dsk

    # Return optimized ddf
    ddf.dask = dsk
    return ddf


class Distributed:
    """Distributed-Execution Context Manager

    The purpose of this execution-manager utility is to
    provide an intuitive context manager for distributed
    (multi-GPU/CPU) scheduling and execution with Dask.

    NOTE: For multi-node execution, it is the users
    responsibility to create a distributed Dask cluster
    with an appropriate deployment technology.  This
    class only supports the automatic generation of
    local (single-machine) clusters. However, the class
    can be used to connect to any existing dask cluster
    (local or not), as long as a valid `client` argument
    can be defined.

    Parameters
    -----------
    client : `dask.distributed.Client`; Optional
        The client to use for distributed-Dask execution.
    cluster_type : {"cuda", "cpu", None}
        Type of local cluster to generate in the case that a
        global client is not detected (or `force_new=True`).
        "cuda" corresponds to `dask_cuda.LocalCUDACluster`,
        while "cpu" corresponds to `distributed.LocalCluster`.
        Default is "cuda" if GPU support is detected.
    force_new : bool
        Whether to force the creation of a new local cluster
        in the case that a global client object is already
        detected. Default is False.
    **cluster_options :
        Key-word arguments to pass to the local-cluster
        constructor specified by `cluster_type` (e.g.
        `n_workers=2`).

    Examples
    --------
    The easiest way to use `Distributed` is within a
    conventional `with` statement::

        from merlin.core.utils import Disributed

        workflow = nvt.Workflow(["col"] >> ops.Normalize())
        dataset = nvt.Dataset(...)
        with Distributed():
            workflow.transform(dataset).to_parquet(...)

    In this case, all Dask-based scheduling and execution
    required within the `with Distributed()` block will be
    performed using a distributed cluster. If an existing
    client is not detected, a default `LocalCUDACluster`
    or `LocalCluster` will be automatically deployed (the
    specific type depending on GPU support).

    Alternatively, the distributed-execution manager can be
    used without a `with` statement as follows::

        workflow = nvt.Workflow(["col"] >> ops.Normalize())
        dataset = nvt.Dataset(...)
        exec = Distributed()
        workflow.transform(dataset).to_parquet(...)
        exec.deactivate()

    Note that `deactivate()` must be used to resume default
    execution in the case that `Distributed` is not used in
    a `with` context.

    Since the default local cluster may be inefficient for
    many workflows, the user can also specify the specific
    `cluster_type` and `**cluster_options`. For example::

        with Distributed(
            cluster_type="cuda",
            force_new=True,  # Ignore existing cluster(s)
            n_workers=4,
            local_directory="/raid/dask-space",
            protocol="ucx",
            device_memory_limit=0.8,
            rmm_pool_size="20GB",
            log_spilling=True,
        ):
            workflow.transform(dataset).to_parquet(...)

    In this case, the `cluster_type="cuda"` calls for the
    creation of a `LocalCUDACluster`, and all other key-word
    arguments are passed to the `LocalCUDACluster` constructor.
    """

    def __init__(self, client=None, cluster_type=None, force_new=False, **cluster_options):
        self._initial_client = global_dask_client()  # Initial state
        self._client = client or "auto"  # Cannot be `None`
        self.cluster_type = cluster_type or ("cuda" if HAS_GPU else "cpu")
        self.cluster_options = cluster_options
        # We can only shut down the cluster in `shutdown`/`__exit__`
        # if we are generating it internally
        set_dask_client(self._client)
        self._allow_shutdown = global_dask_client() is None or force_new
        self._active = False
        self.force_new = force_new
        # Activate/deploy the client/cluster
        self._activate()

    @property
    def client(self):
        return self._client

    @property
    def cluster(self):
        return self.client.cluster

    @property
    def dashboard_link(self):
        return self.client.dashboard_link

    def _activate(self):
        if not self._active:
            self._client = set_dask_client(
                self._client,
                new_cluster=self.cluster_type,
                force_new=self.force_new,
                **self.cluster_options,
            )
        self._active = True
        if self._client in ("auto", None):
            raise RuntimeError(f"Failed to deploy a new local {self.cluster_type} cluster.")

    def _deactivate(self):
        self._client = set_dask_client(self._initial_client)
        self._active = False

    def deactivate(self):
        if self._allow_shutdown and self._active:
            self._client.close()
        self._deactivate()

    def __enter__(self):
        self._activate()
        return self

    def __exit__(self, *args):
        self.deactivate()

    def __del__(self, *args):
        self.deactivate()


class Serial:
    """Serial-Execution Context Manager

    Examples
    --------
    The easiest way to use `Serial` is within a
    conventional `with` statement::

        from merlin.core.utils import Serial

        workflow = nvt.Workflow(["col"] >> ops.Normalize())
        dataset = nvt.Dataset(...)
        with Serial():
            workflow.transform(dataset).to_parquet(...)

    In this case, all Dask-based scheduling and execution
    required within the `with Serial()` block will be
    performed using the "synchronous" (single-threaded)
    scheduler.

    Alternatively, the serial-execution manager can be
    used without a `with` statement as follows::

        workflow = nvt.Workflow(["col"] >> ops.Normalize())
        dataset = nvt.Dataset(...)
        exec = Serial()
        workflow.transform(dataset).to_parquet(...)
        exec.deactivate()

    Note that `deactivate()` must be used to resume
    default execution in the case that `Serial` is
    not used in a `with` context.
    """

    def __init__(self):
        # Save the initial client setting and
        # activate serial-execution mode
        self._initial_client = global_dask_client()
        self._client = self._initial_client
        self._active = False
        self._activate()

    @property
    def client(self):
        return self._client

    def _activate(self):
        # Activate serial-execution mode.
        # This just means we are setting the
        # global dask client to `None`
        if not self._active:
            set_dask_client(None)
        self._active = True
        if global_dask_client() is not None:
            raise RuntimeError("Failed to activate serial-execution mode.")

    def deactivate(self):
        # Deactivate serial-execution mode.
        # This just means we are setting the
        # global dask client to the original setting.
        set_dask_client(self._initial_client)
        self._active = False
        if self._initial_client is not None and global_dask_client() is None:
            raise RuntimeError("Failed to deactivate serial-execution mode.")

    def __enter__(self):
        self._activate()
        return self

    def __exit__(self, *args):
        self.deactivate()


def set_client_deprecated(client, caller_str):
    warnings.warn(
        f"The `client` argument is deprecated from {caller_str} "
        f"and will be removed in a future version of NVTabular. By "
        f"default, a global client in the same python context will be "
        f"detected automatically, and `merlin.utils.set_dask_client` "
        f"(as well as `Distributed` and `Serial`) can be used for "
        f"explicit control.",
        FutureWarning,
    )
    set_dask_client(client)


def set_dask_client(client="auto", new_cluster=None, force_new=False, **cluster_options):
    """Set the Dask-Distributed client.

    Parameters
    -----------
    client : {"auto", None} or `dask.distributed.Client`
        The client to use for distributed-Dask execution.
        If `"auto"` (default) the current python context will
        be searched for an existing client object. Specify
        `None` to disable distributed execution altogether.
    new_cluster : {"cuda", "cpu", None}
        Type of local cluster to generate in the case that
        `client="auto"` and a global dask client is not
        detected in the current python context. The "cuda"
        option corresponds to `dask_cuda.LocalCUDACluster`,
        while "cpu" corresponds to `distributed.LocalCluster`.
        Default is `None` (no local cluster is generated).
    force_new : bool
        Whether to force the creation of a new local cluster
        in the case that a global client object is already
        detected. Default is False.
    **cluster_options :
        Key-word arguments to pass to the local-cluster
        constructor specified by `new_cluster` (e.g.
        `n_workers=2`).
    """
    _merlin_dask_client.set(client)

    # Check if we need to deploy a new cluster
    if new_cluster and client is not None:
        base, cluster = {
            "cuda": ("dask_cuda", "LocalCUDACluster"),
            "cpu": ("distributed", "LocalCluster"),
        }.get(new_cluster, (None, None))
        if global_dask_client() is not None and not force_new:
            # Don't deploy a new cluster if one already exists
            warnings.warn(
                f"Existing Dask-client object detected in the "
                f"current context. New {new_cluster} cluster "
                f"will not be deployed. Set force_new to True "
                f"to ignore running clusters."
            )
        elif base and cluster:
            try:
                base = importlib.import_module(base)
            except ImportError as err:
                # ImportError should only occur for LocalCUDACluster,
                # but I'm making this general to be "safe"
                raise ImportError(
                    f"new_cluster={new_cluster} requires {base}. "
                    f"Please make sure this library is installed. "
                ) from err
            _merlin_dask_client.set(Client(getattr(base, cluster)(**cluster_options)))
        else:
            # Something other than "cuda" or "cpu" was specified
            raise ValueError(f"{new_cluster} not a supported option for new_cluster.")

    # Return the active client object
    active = _merlin_dask_client.get()
    return None if active == "auto" else active


def global_dask_client() -> Optional[distributed.Client]:
    """Get Global Dask client if it's been set.

    Returns
    -------
    Optional[distributed.Client]
        The global client.
    """
    # First, check _merlin_dask_client
    merlin_client = _merlin_dask_client.get()
    if merlin_client and merlin_client != "auto":
        if merlin_client.cluster and merlin_client.cluster.workers:  # type: ignore
            # Active Dask client already known
            return merlin_client
        else:
            # Our cached client is no-longer
            # active, reset to "auto"
            merlin_client = "auto"
    if merlin_client == "auto":
        try:
            # Check for a global Dask client
            set_dask_client(get_client())
            return _merlin_dask_client.get()
        except ValueError:
            # no global client found
            pass
    # Catch-all
    return None


def run_on_worker(func: Callable, *args, **kwargs) -> Any:
    """Run a function on a Dask worker using `delayed`
    execution (if a Dask client is detected)

    Parameters
    ----------
    func : Callable
        The function to run

    Returns
    -------
    Any
        The result of the function call with supplied arguments
    """

    if global_dask_client():
        # There is a specified or global Dask client. Use it
        return dask.delayed(func)(*args, **kwargs).compute()
    # No Dask client - Use simple function call
    return func(*args, **kwargs)
