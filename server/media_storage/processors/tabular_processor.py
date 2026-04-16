import csv
import io
import logging
from typing import List, Optional

from .base import BaseFileProcessor, ProcessorResult

logger = logging.getLogger(__name__)


class TabularProcessor(BaseFileProcessor):
    async def run_baseline(
        self,
        file_bytes: bytes,
        media_file,
        tier: int,
        analysis_kinds: List[str],
        call_context: Optional[object] = None,
    ) -> List[ProcessorResult]:
        if tier != 0:
            return []

        from media_storage.constants import MAX_TABULAR_COLUMNS, MAX_TABULAR_ROWS_PROFILE

        dataframe, parse_info = self.parse_dataframe(file_bytes, media_file)
        if dataframe is None:
            return []

        profiled = dataframe
        if len(profiled.columns) > MAX_TABULAR_COLUMNS:
            profiled = profiled.iloc[:, :MAX_TABULAR_COLUMNS]
            parse_info['columns_truncated'] = True

        if len(profiled) > MAX_TABULAR_ROWS_PROFILE:
            profiled = profiled.head(MAX_TABULAR_ROWS_PROFILE)
            parse_info['rows_truncated'] = True

        results: list[ProcessorResult] = []
        if 'tabular_schema_profile' in analysis_kinds:
            results.append(self._schema_profile(profiled, dataframe, parse_info))
        if 'tabular_sample_rows' in analysis_kinds:
            results.append(self._sample_rows(dataframe))
        return results

    def parse_dataframe(self, file_bytes: bytes, media_file, sheet_name=None):
        import pandas as pd

        content_type = (media_file.content_type or '').lower()
        filename = (media_file.original_filename or '').lower()
        parse_info: dict = {}

        if (
            content_type == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            or filename.endswith('.xlsx')
        ):
            try:
                excel_file = pd.ExcelFile(io.BytesIO(file_bytes), engine='openpyxl')
                parse_info['sheet_count'] = len(excel_file.sheet_names)
                parse_info['sheet_names'] = excel_file.sheet_names[:20]
                target_sheet = sheet_name if sheet_name is not None else 0
                dataframe = pd.read_excel(excel_file, sheet_name=target_sheet, nrows=50_000)
                parse_info['active_sheet'] = (
                    sheet_name if sheet_name is not None else excel_file.sheet_names[0]
                )
                return dataframe, parse_info
            except Exception as exc:
                logger.warning('XLSX parse failed for %s: %s', media_file.uuid, exc)
                return None, {}

        text = None
        for encoding in ('utf-8', 'utf-8-sig', 'latin-1', 'cp1252', 'cp1251', 'windows-1251'):
            try:
                text = file_bytes.decode(encoding)
                parse_info['encoding'] = encoding
                break
            except (UnicodeDecodeError, ValueError):
                continue

        if text is None:
            logger.warning('Unable to decode tabular file %s', media_file.uuid)
            return None, {}

        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = '\t' if (
                content_type == 'text/tab-separated-values'
                or filename.endswith('.tsv')
            ) else ','
        parse_info['delimiter'] = delimiter

        try:
            has_header = csv.Sniffer().has_header(text[:4096])
        except csv.Error:
            has_header = True
        parse_info['has_header'] = has_header

        try:
            dataframe = pd.read_csv(
                io.StringIO(text),
                sep=delimiter,
                header=0 if has_header else None,
                nrows=50_000,
                on_bad_lines='skip',
            )
        except Exception as exc:
            logger.warning('CSV/TSV parse failed for %s: %s', media_file.uuid, exc)
            return None, {}

        return dataframe, parse_info

    def dataframe_to_csv_text(self, dataframe) -> str:
        try:
            cleaned = dataframe.fillna('')
        except Exception:
            cleaned = dataframe
        return cleaned.to_csv(index=False)

    def _schema_profile(self, df_profile, df_full, parse_info: dict) -> ProcessorResult:
        import pandas as pd

        columns = []
        for column in df_profile.columns:
            column_info = {
                'name': str(column),
                'dtype': str(df_profile[column].dtype),
                'null_count': int(df_profile[column].isna().sum()),
                'null_pct': round(df_profile[column].isna().mean() * 100, 1),
                'unique_count': int(df_profile[column].nunique()),
            }
            if pd.api.types.is_numeric_dtype(df_profile[column]):
                description = df_profile[column].describe()
                column_info['min'] = float(description.get('min', 0))
                column_info['max'] = float(description.get('max', 0))
                column_info['mean'] = round(float(description.get('mean', 0)), 2)
            elif df_profile[column].dtype == object:
                non_null = df_profile[column].dropna()
                if len(non_null) > 0:
                    column_info['sample_values'] = [str(value)[:100] for value in non_null.head(3).tolist()]
            columns.append(column_info)

        result = {
            'row_count': len(df_full),
            'column_count': len(df_full.columns),
            'columns': columns[:200],
            'parse_info': parse_info,
        }
        summary = f'Tabular: {len(df_full):,} rows x {len(df_full.columns)} columns'
        if parse_info.get('sheet_count', 0) > 1:
            summary += f' ({parse_info["sheet_count"]} sheets)'
        return ProcessorResult(
            analysis_kind='tabular_schema_profile',
            summary_text=summary,
            result_json=result,
        )

    def _sample_rows(self, dataframe) -> ProcessorResult:
        head = dataframe.head(5).fillna('').to_dict(orient='records')
        tail = dataframe.tail(5).fillna('').to_dict(orient='records') if len(dataframe) > 5 else []

        def truncate_record(record: dict) -> dict:
            return {key: str(value)[:500] for key, value in record.items()}

        result = {
            'head': [truncate_record(record) for record in head],
            'tail': [truncate_record(record) for record in tail],
            'total_rows': len(dataframe),
        }
        return ProcessorResult(
            analysis_kind='tabular_sample_rows',
            summary_text=f'Sample: {len(head)} head + {len(tail)} tail rows',
            result_json=result,
        )
