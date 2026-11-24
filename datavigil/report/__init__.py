from .builder import build_report_data, build_schema_report_data
from .docx_render import render_docx
from .pdf_render import render_pdf
from .schema_render import render_schema_docx, render_schema_pdf


def render_report(data: dict, path: str, fmt: str) -> str:
    """Render an audit bundle to `path` in `fmt` ('docx' or 'pdf')."""
    fmt = fmt.lower().lstrip(".")
    if data.get("is_schema_audit"):
        if fmt == "docx":
            return render_schema_docx(data, path)
        if fmt == "pdf":
            return render_schema_pdf(data, path)
        raise ValueError(f"Unsupported report format: {fmt} (use docx or pdf)")
    if fmt == "docx":
        return render_docx(data, path)
    if fmt == "pdf":
        return render_pdf(data, path)
    raise ValueError(f"Unsupported report format: {fmt} (use docx or pdf)")


__all__ = ["build_report_data", "build_schema_report_data",
           "render_docx", "render_pdf",
           "render_schema_docx", "render_schema_pdf", "render_report"]
