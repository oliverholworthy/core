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
import numpy as np

from merlin.core.dispatch import is_string_dtype
from merlin.dtypes.mapping import DTypeMapping, NumpyPreprocessor
from merlin.dtypes.registry import _dtype_registry


def cudf_translator(raw_dtype) -> np.dtype:
    category_type = raw_dtype._categories.dtype
    if is_string_dtype(category_type):
        return np.dtype("str")
    else:
        return category_type


try:
    # We only want to register this mapping if cudf is available, even though
    # the mapping itself doesn't use cudf (yet?)

    import cudf  # pylint:disable=unused-import # noqa: F401

    cudf_dtypes = DTypeMapping(
        translator=NumpyPreprocessor("cudf", cudf_translator, attrs=["_categories"]),
    )
    _dtype_registry.register("cudf", cudf_dtypes)
except ImportError as exc:
    from warnings import warn

    warn(f"cuDF dtype mappings did not load successfully due to an error: {exc.msg}")
