from __future__ import annotations

import io
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd
import requests

DEFAULT_GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1SLIC1gLgD6z7_LhIkxXA8mHX5RBv-cKn/edit?gid=820290981#gid=820290981"
DEFAULT_SHEET_NAME = "Tuần này"
CACHE_TTL_SECONDS = 600

# Không trả ngày sinh, điện thoại, email cá nhân, CCCD, ngân hàng, địa chỉ hoặc lương.
SAFE_COLUMNS = {
    "name": "2. Họ và tên*",
    "code": "3. Mã nhân viên*",
    "company_email": "4. Email công ty*",
    "department": "23. Phòng ban*",
    "position": "24. Vị trí công việc*",
    "employee_type": "25. Loại hình nhân sự*",
    "status": "27. Trạng thái nhân sự*",
    "start_date": "29. Ngày bắt đầu đi làm*",
    "manager": "37. Quản lý trực tiếp",
}
_CACHE: tuple[float, pd.DataFrame] | None = None


class PersonnelLookupError(RuntimeError):
    pass


@dataclass(frozen=True)
class PersonnelLookupResult:
    handled: bool
    answer: str | None = None


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFD", str(value or "").casefold())
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    return re.sub(r"\s+", " ", text.replace("đ", "d")).strip()


def _clean_cell(value: Any) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return re.sub(r"\s+", " ", str(value)).strip()


def _export_url(url: str) -> str:
    match = re.search(r"/spreadsheets/d/([^/]+)", url)
    if not match:
        raise PersonnelLookupError("Link Google Sheets không hợp lệ.")
    return f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=xlsx"


def _find_header_row(raw: pd.DataFrame) -> int:
    expected = _normalize(SAFE_COLUMNS["name"])
    for row_index in range(min(len(raw), 30)):
        if expected in {_normalize(value) for value in raw.iloc[row_index].tolist()}:
            return row_index
    raise PersonnelLookupError("Không tìm thấy dòng tiêu đề danh sách nhân sự.")


def _load_people(force: bool = False) -> pd.DataFrame:
    global _CACHE
    now = time.monotonic()
    if not force and _CACHE and now - _CACHE[0] < CACHE_TTL_SECONDS:
        return _CACHE[1]
    source_url = os.getenv("PERSONNEL_GOOGLE_SHEET_URL", DEFAULT_GOOGLE_SHEET_URL).strip()
    sheet_name = os.getenv("PERSONNEL_SHEET_NAME", DEFAULT_SHEET_NAME).strip()
    try:
        response = requests.get(_export_url(source_url), timeout=45)
        response.raise_for_status()
        if response.content[:2] != b"PK":
            raise PersonnelLookupError("Google Sheets không trả về workbook Excel.")
        raw = pd.read_excel(io.BytesIO(response.content), sheet_name=sheet_name, header=None, dtype=object)
    except (requests.RequestException, ValueError, OSError) as exc:
        raise PersonnelLookupError(f"Không tải được danh sách nhân sự: {exc}") from exc
    header_row = _find_header_row(raw)
    people = raw.iloc[header_row + 1 :].copy()
    people.columns = [_clean_cell(value) for value in raw.iloc[header_row].tolist()]
    missing = [column for column in SAFE_COLUMNS.values() if column not in people.columns]
    if missing:
        raise PersonnelLookupError("Danh sách nhân sự thiếu cột: " + ", ".join(missing))
    people = people[list(SAFE_COLUMNS.values())].rename(columns={value: key for key, value in SAFE_COLUMNS.items()})
    people = people.loc[people["name"].notna()].copy()
    people["_name_key"] = people["name"].map(_normalize)
    people = people.loc[people["_name_key"].ne("")].reset_index(drop=True)
    _CACHE = (now, people)
    return people


def _extract_name_query(query: str) -> str | None:
    normalized = _normalize(query)
    patterns = (
        r"^(?:cho (?:toi|minh|em) (?:hoi )?)?(.+?) la ai[?.!]*$",
        r"^(?:thong tin|tim|tra cuu) (?:nhan su|nhan vien)?\s*(.+?)[?.!]*$",
        r"^(.+?) (?:lam o phong nao|giu vi tri gi|chuc vu gi)[?.!]*$",
    )
    for pattern in patterns:
        match = re.match(pattern, normalized)
        if match:
            candidate = match.group(1).strip()
            if candidate and len(candidate.split()) <= 8:
                return candidate
    return None


def _matches_name(name_key: str, search_name: str) -> bool:
    search_tokens, name_tokens = search_name.split(), name_key.split()
    return bool(search_tokens) and (search_tokens[0] in name_tokens if len(search_tokens) == 1 else all(token in name_tokens for token in search_tokens))


def _format_person(row: pd.Series) -> list[str]:
    name, code = _clean_cell(row["name"]), _clean_cell(row["code"])
    lines = [f"- **{name}**" + (f" ({code})" if code else "")]
    for label, key in (("Phòng ban", "department"), ("Vị trí", "position"), ("Email công ty", "company_email"), ("Loại hình", "employee_type"), ("Trạng thái", "status"), ("Ngày vào làm", "start_date"), ("Quản lý trực tiếp", "manager")):
        value = _clean_cell(row[key])
        if value:
            lines.append(f"  - {label}: {value}")
    return lines


def lookup_personnel_query(query: str) -> PersonnelLookupResult:
    search_name = _extract_name_query(query)
    if not search_name:
        return PersonnelLookupResult(handled=False)
    try:
        people = _load_people()
    except PersonnelLookupError:
        return PersonnelLookupResult(True, "Chưa thể truy cập Flexfit Danh sách nhân sự lúc này. Bạn vui lòng thử lại sau.")
    matches = people.loc[people["_name_key"].map(lambda name: _matches_name(name, search_name))]
    if matches.empty:
        return PersonnelLookupResult(True, f"Không tìm thấy nhân sự phù hợp với “{search_name}” trong sheet “Tuần này” của Flexfit Danh sách nhân sự.")
    lines = [f"Có {len(matches)} nhân sự khớp với “{search_name}”:" if len(matches) > 1 else f"Thông tin nhân sự khớp với “{search_name}”:", ""]
    for _, row in matches.iterrows():
        lines.extend(_format_person(row))
    lines.extend(["", "Nguồn: Flexfit Danh sách nhân sự, sheet “Tuần này”."])
    return PersonnelLookupResult(True, "\n".join(lines))
