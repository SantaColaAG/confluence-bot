import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag


@dataclass
class Mirror:
    name: str
    default_allow: bool
    deny: list[str] = field(default_factory=list)


@dataclass
class ProjectData:
    redirector: dict[str, str] = field(default_factory=dict)
    mirrors: dict[str, Mirror] = field(default_factory=dict)


def _cell_text(cell: Tag) -> str:
    parts: list[str] = []
    for macro in cell.find_all(lambda t: isinstance(t, Tag) and t.name and "structured-macro" in t.name):
        for param in macro.find_all(lambda t: isinstance(t, Tag) and t.name and "parameter" in t.name):
            if param.get("ac:name") == "title":
                parts.append(param.get_text(" ", strip=True))
        macro.extract()
    tail = cell.get_text(" ", strip=True)
    if tail:
        parts.append(tail)
    return " ".join(p for p in parts if p)


def _headers(row: Tag) -> list[str]:
    return [_cell_text(c).lower() for c in row.find_all(["th", "td"])]


def parse_project(html: str) -> ProjectData:
    soup = BeautifulSoup(html, "html.parser")
    data = ProjectData()

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = _headers(rows[0])

        if "geo" in headers and "target" in headers and len(headers) <= 3:
            g_idx = headers.index("geo")
            t_idx = headers.index("target")
            for row in rows[1:]:
                cells = row.find_all(["th", "td"])
                if len(cells) <= max(g_idx, t_idx):
                    continue
                geo = _cell_text(cells[g_idx]).strip().lower()
                target = _cell_text(cells[t_idx]).strip()
                if geo and target:
                    data.redirector[geo] = target

        elif "mirror" in headers:
            m_idx = headers.index("mirror")
            default_idx = next((i for i, h in enumerate(headers) if "default" in h), None)
            deny_idx = next((i for i, h in enumerate(headers) if "deny" in h), None)
            for row in rows[1:]:
                cells = row.find_all(["th", "td"])
                if len(cells) <= m_idx:
                    continue
                name = _cell_text(cells[m_idx]).strip()
                if not name:
                    continue
                default_allow = False
                if default_idx is not None and len(cells) > default_idx:
                    default_allow = "allow" in _cell_text(cells[default_idx]).lower()
                deny: list[str] = []
                if deny_idx is not None and len(cells) > deny_idx:
                    deny_text = _cell_text(cells[deny_idx])
                    deny = re.findall(r"\b[A-Z]{2}\b", deny_text)
                data.mirrors[name] = Mirror(name=name, default_allow=default_allow, deny=deny)

    return data
