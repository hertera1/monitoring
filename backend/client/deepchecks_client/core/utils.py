# ----------------------------------------------------------------------------
# Copyright (C) 2021-2022 Deepchecks (https://www.deepchecks.com)
#
# This file is part of Deepchecks.
# Deepchecks is distributed under the terms of the GNU Affero General
# Public License (version 3 or later).
# You should have received a copy of the GNU Affero General Public License
# along with Deepchecks.  If not, see <http://www.gnu.org/licenses/>.
# ----------------------------------------------------------------------------
#
# pylint: disable=import-outside-toplevel
"""Module containing deepchecks monitoring client."""
import enum
import json
import typing as t
import warnings
from datetime import datetime
from json import JSONDecodeError
from numbers import Number

import httpx
import numpy as np
import pandas as pd
import pendulum as pdl
import rfc3339_validator
from deepchecks.tabular import Dataset
from deepchecks.utils.type_inference import is_categorical
from pandas.core.dtypes.common import (is_bool_dtype, is_categorical_dtype, is_datetime64_dtype, is_integer_dtype,
                                       is_numeric_dtype, is_period_dtype)
from pendulum.datetime import DateTime as PendulumDateTime
from termcolor import cprint
from typing_extensions import TypeAlias, TypedDict

__all__ = ['ColumnType', 'ColumnTypeName', 'TaskType', 'DeepchecksColumns',
           'validate_additional_data_schema', 'describe_dataset', 'DataSchema',
           'DataFilter', 'OperatorsEnum', 'classification_label_formatter', 'validate_frequency']

ColumnTypeName: TypeAlias = str


class TaskType(enum.Enum):
    """Enum containing supported task types."""

    REGRESSION = 'regression'
    MULTICLASS = 'multiclass'
    BINARY = 'binary'

    @classmethod
    def values(cls):
        return [e.value for e in TaskType]

    @classmethod
    def tabular_types(cls):
        return {cls.REGRESSION, cls.MULTICLASS, cls.BINARY}

    @classmethod
    def convert(cls, task_type: t.Any) -> 'TaskType':
        if isinstance(task_type, cls):
            return task_type
        if isinstance(task_type, str):
            if task_type in cls.values():
                return cls(task_type)
            else:
                raise ValueError(f'Unknown task type - {task_type}. Possible values are {cls.values()}')

        from deepchecks_client.tabular import DeepchecksTaskType as TabularTaskType

        if isinstance(task_type, TabularTaskType):
            return cls(task_type.value)

        raise ValueError(
            f'Unknown value type - {type(task_type)}, '
            'cannot convert it into TaskType'
        )


class ColumnType(str, enum.Enum):
    """Enum containing possible types of data."""

    NUMERIC = 'numeric'
    INTEGER = 'integer'
    BIGINT = 'bigint'
    CATEGORICAL = 'categorical'
    BOOLEAN = 'boolean'
    TEXT = 'text'
    ARRAY_FLOAT = 'array_float'
    ARRAY_FLOAT_2D = 'array_float_2d'
    DATETIME = 'datetime'

    @classmethod
    def values(cls):
        return [it.value for it in cls]


class DeepchecksColumns(str, enum.Enum):
    """Enum of saved deepchecks columns."""

    SAMPLE_ID_COL = '_dc_sample_id'
    SAMPLE_TS_COL = '_dc_time'
    SAMPLE_LABEL_COL = '_dc_label'
    SAMPLE_PRED_PROBA_COL = '_dc_prediction_probabilities'
    SAMPLE_PRED_COL = '_dc_prediction'


class OperatorsEnum(str, enum.Enum):
    """Operators for numeric and categorical filters."""

    GE = 'greater_than_equals'
    GT = 'greater_than'
    LE = 'less_than_equals'
    LT = 'less_than'
    CONTAINS = 'contains'
    EQ = 'equals'
    NOT_EQ = 'not_equals'


class DataFilter(TypedDict):
    """A dictionary that defines a filter for a dataframe.

    Parameters
    ----------
    column : str
        The name of the column to filter on, column can be a feature or an additional-data column.
    operator : OperatorsEnum
        The operator to use for the filter by the value.
    value : t.Any
        The value to use as reference.

    Examples
    --------
    Data filter to filter out values smaller or equal to 5 on the column 'feature1'

    >>> from deepchecks_client import DataFilter, OperatorsEnum
    >>> filter = DataFilter(column='feature1', operator=OperatorsEnum.GE, value=5)

    """
    column: str
    operator: OperatorsEnum
    value: t.Union[int, str]


def maybe_raise(
        response: httpx.Response,
        expected: t.Union[int, t.Tuple[int, int]] = (200, 299),
        msg: t.Optional[str] = None
) -> httpx.Response:
    """Verify response status and raise an HTTPError if got unexpected status code.

    Parameters
    ==========
    response : Response
        http response instance
    expected : Union[int, Tuple[int, int]] , default (200, 299)
        HTTP status code that is expected to receive
    msg : Optional[str] , default None
        error message to show in case of unexpected status code,
        next template parameters available:
        - status (HTTP status code)
        - reason (HTTP reason message)
        - url (request url)
        - body (response payload if available)
        - error (default error message that will include all previous parameters)

    Returns
    =======
    Response
    """
    status = response.status_code
    url = response.url
    reason = response.content

    error_template = 'Error: {status} {reason} url {url}.\nBody:\n{body}'
    client_error_template = '{status} Client Error: {reason} for url: {url}.\nBody:\n{body}'

    server_error_template = (
        '{status} Server Internal Error: {reason} for url: {url}.\n'
        'Please, reach Deepchecks support team for more information about this problem.\n'
        'Body:\n{body}'
    )

    def select_template(status):
        if 400 <= status <= 499:
            return client_error_template
        elif 500 <= status <= 599:
            return server_error_template
        else:
            return error_template

    def process_body():
        try:
            return json.dumps(response.json(), indent=3)
        except JSONDecodeError:
            return

    if isinstance(expected, int) and status != expected:
        body = process_body()
        error = select_template(status).format(status=status, reason=reason, url=url, body=body)
        raise httpx.HTTPStatusError(
            error if msg is None else msg.format(
                status=status,
                reason=reason,
                url=url,
                body=body,
                error=error
            ),
            request=response.request,
            response=response
        )

    if isinstance(expected, (tuple, list)) and not expected[0] <= status <= expected[1]:
        body = process_body()
        error = select_template(status).format(status=status, reason=reason, url=url, body=body)
        raise httpx.HTTPStatusError(
            error if msg is None else msg.format(
                status=status,
                reason=reason,
                url=url,
                body=body,
                error=error
            ),
            request=response.request,
            response=response

        )

    return response


def validate_additional_data_schema(additional_data: t.Dict[str, ColumnTypeName],
                                    features: t.Dict[str, ColumnTypeName]):
    if additional_data is not None:
        if not isinstance(additional_data, dict):
            raise ValueError('additional_data_schema must be a dict')
        intersection = set(additional_data.keys()).intersection(features.keys())
        if intersection:
            raise ValueError(f'features and additional_data must contain different keys, found shared keys: '
                             f'{intersection}')
        for key, value in features.items():
            if not isinstance(key, str):
                raise ValueError(f'key of additional_data_schema must be of type str but got: {type(key)}')
            if value not in ColumnType.values():
                raise ValueError(
                    f'value of additional_data_schema must be one of {ColumnType.values()} but got {value}')


class DeepchecksEncoder:
    """Deepchecks encoder."""

    @classmethod
    def encode(cls, obj):
        if isinstance(obj, np.generic):
            return obj.item()
        if isinstance(obj, np.ndarray):
            return cls.encode(obj.tolist())
        if isinstance(obj, dict):
            return {k: cls.encode(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return tuple(cls.encode(v) for v in obj)
        if pd.isna(obj):
            return None

        return obj


def parse_timestamp(timestamp: t.Union[int, datetime, str]) -> 'PendulumDateTime':
    """Parse timestamp to datetime object."""
    # If no timezone in datetime, assumed to be UTC and converted to local timezone
    if isinstance(timestamp, int) or np.issubdtype(type(timestamp), np.integer):
        return pdl.from_timestamp(timestamp, pdl.local_timezone())
    elif isinstance(timestamp, PendulumDateTime):
        return timestamp
    elif isinstance(timestamp, datetime):
        return pdl.instance(timestamp, pdl.local_timezone())
    elif isinstance(timestamp, str):
        if rfc3339_validator.validate_rfc3339(timestamp):
            return pdl.parse(timestamp)
        else:
            raise ValueError(f'Not supported timestamp format for: {timestamp}')
    else:
        raise ValueError(f'Not supported timestamp type: {type(timestamp)}')


def pretty_print(msg: str):
    """Pretty print the attached massage to the user terminal.

    Used for information massages which are not errors or warnings."""
    cprint(msg, 'green', attrs=['bold'])


def _get_series_column_type(series: pd.Series):
    if series.dtype == 'object':
        # object might still be only of one type, so we re-infer the dtype
        series = pd.Series(series.dropna().to_list(), name=series.name)
    if is_bool_dtype(series):
        return ColumnType.BOOLEAN.value
    if is_integer_dtype(series):
        if is_categorical(series):
            return ColumnType.CATEGORICAL.value
        if series.abs().ge(2 ** 32).to_numpy().any():
            return ColumnType.BIGINT.value
        return ColumnType.INTEGER.value
    if is_numeric_dtype(series):
        return ColumnType.NUMERIC.value
    if is_categorical_dtype(series):
        return ColumnType.CATEGORICAL.value
    series_types = series.apply(type)
    if series_types.eq(str).all():
        if is_categorical(series):
            return ColumnType.CATEGORICAL.value
        return ColumnType.TEXT.value
    if series_types.eq(datetime).all() or is_datetime64_dtype(series) or is_period_dtype(series):
        return ColumnType.DATETIME.value
    warnings.warn(f'Column {series.name} is of unsupported dtype - {series.dtype}.')
    return None


class DataSchema(TypedDict):
    """Data schema description."""

    features: t.Dict[str, ColumnTypeName]
    additional_data: t.Dict[str, ColumnTypeName]


def describe_dataset(dataset: Dataset) -> DataSchema:
    """Create a schema for a dataset"""
    additional_data = {}
    features = {}
    for column in dataset.data.columns:
        col_series = dataset.data[column]
        if column in [dataset.index_name, dataset.datetime_name]:
            continue
        elif dataset.has_label() and column == dataset.label_name:
            continue
        elif column in dataset.features:
            if column in dataset.cat_features:
                features[column] = (
                    ColumnType.BOOLEAN.value
                    if is_bool_dtype(col_series)
                    else ColumnType.CATEGORICAL.value
                )
            elif column in dataset.numerical_features:
                features[column] = (
                    ColumnType.INTEGER.value
                    if is_integer_dtype(col_series)
                    else ColumnType.NUMERIC.value
                )
            else:
                features[column] = _get_series_column_type(col_series)
                if features[column] == ColumnType.CATEGORICAL.value:
                    features[column] = ColumnType.TEXT.value
        else:
            additional_data[column] = _get_series_column_type(col_series)
    # if any columns failed to auto infer print this warnings
    # moved to here to not annoy the user so much
    if any(x is None for x in list(features.values()) + list(additional_data.values())):
        warnings.warn('Supported dtypes for auto infer are '
                      'numerical, integer, bigint, boolean, string and categorical.\n'
                      'You can set the type manually in the schema file/dict.\n'
                      'DateTime format is supported using iso format only.')
    return {'features': features, 'additional_data': additional_data}


def classification_label_formatter(label):
    """Converts a label to a string, or returns None if the label is NaN."""
    if pd.isna(label):
        return None
    if isinstance(label, Number) and int(label) == label:
        label = int(label)
    return str(label)


def validate_frequency(value) -> str:
    if isinstance(value, int):
        if value == 3600:
            return 'HOUR'
        elif value == 86400:
            return 'DAY'
        elif value == 604800:
            return 'WEEK'
        elif value == 2592000:
            return 'MONTH'
        else:
            # TODO: better message
            raise ValueError(f'Unsupported value of frequency - {value}')

    if isinstance(value, str) and value.lower() in {'hour', 'day', 'week', 'month'}:
        return value.upper()

    raise ValueError(f'Unexpecte value - {value}')
