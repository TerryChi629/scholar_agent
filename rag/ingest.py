"""文献入库: PDF -> 解析 -> 语义分块 -> embedding -> 向量库。

分块策略 (M1):
- 用 PyMuPDF 的 dict 模式拿到每行文本 + 字号 + 页码。
- 以"字号明显大于正文"识别章节标题, 据此把全文切成 section (Abstract/Introduction/...)。
- section 内再按字符预算二次切分, 控制单 chunk 长度; 切分不跨 section。
- 每个 chunk 保留 section 名 + 起始页码 -> 供 evidence_spans 回溯。

元数据抽取:
- title: 首页最大字号的连续文本行。
- year: 优先 PDF creationDate, 回退正文中的 arXiv 编号 / 4 位年份。
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from rag.store import Chunk, get_store


def _paper_id(path: Path) -> str:
    return hashlib.md5(str(path).encode()).hexdigest()[:12]


@dataclass
class _Line:
    page: int
    text: str
    size: float


def _extract_lines(doc) -> list[_Line]:
    """逐页用 dict 模式抽取每行文本与最大字号。"""
    lines: list[_Line] = []
    for pno, page in enumerate(doc):
        data = page.get_text("dict")
        for block in data.get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(s["text"] for s in spans).strip()
                if not text:
                    continue
                size = round(max(s["size"] for s in spans), 1)
                lines.append(_Line(page=pno, text=text, size=size))
    return lines


def _body_size(lines: list[_Line]) -> float:
    """正文字号 = 承载字符最多的字号 (出现频率按文本长度加权)。"""
    counter: Counter[float] = Counter()
    for ln in lines:
        counter[ln.size] += len(ln.text)
    return counter.most_common(1)[0][0] if counter else 10.0


# 常见章节标题关键词 (大小写不敏感), 用于在字号判定外补充识别。
_SECTION_KEYWORDS = re.compile(
    r"^(abstract|introduction|related work|background|preliminaries|"
    r"method(s|ology)?|approach|model|framework|experiment(s)?|evaluation|"
    r"result(s)?|analysis|discussion|conclusion(s)?|references|appendix|acknowledg)",
    re.IGNORECASE,
)


# 噪声行 (作者署名 / 邮箱 / arXiv 水印 / 脚注), 不应作为标题或章节。
_NOISE_LINE = re.compile(r"@|[∗*†‡]\s*$|arXiv:\d", re.IGNORECASE)


def _is_heading(text: str, size: float, body: float) -> bool:
    """判定一行是否为章节标题: 字号显著大于正文且文本短; 或命中章节关键词。"""
    if len(text) > 60 or not re.search(r"[A-Za-z]", text):
        return False
    if _NOISE_LINE.search(text):
        return False
    big = size >= body * 1.15
    # 形如 "1 Introduction" / "3.1 Method" / "INTRODUCTION"
    numbered = bool(re.match(r"^\d{1,2}(\.\d{1,2})*\.?\s+\S", text))
    keyword = bool(_SECTION_KEYWORDS.match(text.lstrip("0123456789. ")))
    return big or (numbered and keyword) or (keyword and len(text) <= 35)


def _split_sections(lines: list[_Line], body: float) -> list[tuple[str, list[_Line]]]:
    """按标题行把文档切成 [(section_name, lines)]。标题前的内容归入 '_front'。"""
    sections: list[tuple[str, list[_Line]]] = []
    cur_name = "_front"
    cur_lines: list[_Line] = []
    for ln in lines:
        if _is_heading(ln.text, ln.size, body):
            if cur_lines:
                sections.append((cur_name, cur_lines))
            cur_name = ln.text
            cur_lines = []
        else:
            cur_lines.append(ln)
    if cur_lines:
        sections.append((cur_name, cur_lines))
    return sections


def _chunk_section(
    section: str, lines: list[_Line], size: int, overlap: int
) -> list[tuple[str, int, str]]:
    """把一个 section 的行按字符预算切成 chunk。

    返回 [(section, page, text)]; page 取该 chunk 首行所在页。不跨 section。
    """
    out: list[tuple[str, int, str]] = []
    buf = ""
    buf_page = lines[0].page if lines else 0
    for ln in lines:
        if not buf:
            buf_page = ln.page
        buf += ln.text + "\n"
        if len(buf) >= size:
            out.append((section, buf_page, buf.strip()))
            buf = buf[-overlap:] if overlap else ""
    if buf.strip():
        out.append((section, buf_page, buf.strip()))
    return out


def parse_pdf_text(path: Path) -> str:
    """用 PyMuPDF 抽取全文纯文本 (保留, 供调试 / 兼容旧调用)。"""
    import fitz  # PyMuPDF

    doc = fitz.open(path)
    return "\n".join(page.get_text() for page in doc)


def _extract_title(lines: list[_Line]) -> str:
    """标题 = 首页最大字号的连续行 (拼接), 跳过 arXiv 水印等噪声。"""
    first_page = [ln for ln in lines if ln.page == 0 and not _NOISE_LINE.search(ln.text)]
    if not first_page:
        return ""
    max_size = max(ln.size for ln in first_page)
    title_lines = [ln.text for ln in first_page if ln.size == max_size]
    title = " ".join(title_lines).strip()
    return title[:300]


def _extract_year(doc, full_text: str) -> int:
    """年份: 优先 PDF creationDate (D:YYYY...), 回退正文 arXiv 编号或 4 位年份。"""
    raw = doc.metadata.get("creationDate") or ""
    m = re.search(r"D:(\d{4})", raw)
    if m:
        y = int(m.group(1))
        if 1990 <= y <= 2100:
            return y
    # arXiv:YYMM.xxxxx -> 20YY
    m = re.search(r"arXiv:(\d{2})\d{2}\.", full_text)
    if m:
        return 2000 + int(m.group(1))
    m = re.search(r"\b(19|20)\d{2}\b", full_text)
    return int(m.group(0)) if m else 0


def parse_pdf(path: Path) -> dict:
    """解析单篇 PDF, 返回 {title, year, chunks:[(section,page,text)]}。"""
    import fitz  # PyMuPDF

    doc = fitz.open(path)
    lines = _extract_lines(doc)
    if not lines:  # OCR 回退占位: 当前直接返回空
        return {"title": path.stem, "year": 0, "chunks": []}

    body = _body_size(lines)
    full_text = "\n".join(ln.text for ln in lines)
    title = _extract_title(lines) or path.stem
    year = _extract_year(doc, full_text)

    chunks: list[tuple[str, int, str]] = []
    for section, sec_lines in _split_sections(lines, body):
        chunks.extend(_chunk_section(section, sec_lines, size=800, overlap=120))
    chunks = [c for c in chunks if c[2].strip()]
    return {"title": title, "year": year, "chunks": chunks}


def ingest_dir(directory: str) -> dict:
    """扫描目录下所有 PDF 入库, 返回统计。"""
    store = get_store()
    root = Path(directory).expanduser()
    pdfs = list(root.glob("**/*.pdf"))
    total_chunks = 0
    papers: list[dict] = []
    for pdf in pdfs:
        pid = _paper_id(pdf)
        parsed = parse_pdf(pdf)
        chunks = [
            Chunk(
                chunk_id=f"{pid}_{i}",
                paper_id=pid,
                text=text,
                metadata={
                    "title": parsed["title"],
                    "year": parsed["year"],
                    "source": str(pdf),
                    "section": section,
                    "page": page,
                    "chunk_index": i,
                },
            )
            for i, (section, page, text) in enumerate(parsed["chunks"])
        ]
        store.add(chunks)
        total_chunks += len(chunks)
        papers.append({"paper_id": pid, "title": parsed["title"],
                       "year": parsed["year"], "chunks": len(chunks)})
    return {"papers": len(pdfs), "chunks": total_chunks, "detail": papers}
