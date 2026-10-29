"""Word -> PDF. Tries Word (Windows), then LibreOffice (Linux server, macOS, Windows)."""
import os
import platform
import shutil
import subprocess

CANDIDATES = [
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]


def find_soffice():
    env = os.environ.get("SOFFICE_CMD")
    if env:
        return env.split()
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return [p]
    for p in CANDIDATES:
        if os.path.exists(p):
            return [p]
    return None


def docx_to_pdf(docx_path, pdf_path, soffice_cmd=None):
    docx_path, pdf_path = os.path.abspath(docx_path), os.path.abspath(pdf_path)
    os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
    errors = []
    if platform.system() == "Windows":
        try:
            import win32com.client
            word = None
            try:
                word = win32com.client.DispatchEx("Word.Application")
                word.Visible = False
                doc = word.Documents.Open(docx_path)
                doc.SaveAs2(pdf_path, FileFormat=17)
                doc.Close(False)
                return "word"
            finally:
                if word is not None:
                    word.Quit()
        except Exception as e:
            errors.append(f"Word: {e}")
    cmd = soffice_cmd or find_soffice()
    if cmd:
        outdir = os.path.dirname(pdf_path)
        c = list(cmd) + ["--headless", "--convert-to", "pdf", "--outdir", outdir, docx_path]
        r = subprocess.run(c, capture_output=True, text=True, timeout=240)
        produced = os.path.join(outdir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
        if os.path.exists(produced):
            if produced != pdf_path:
                shutil.move(produced, pdf_path)
            return "libreoffice"
        errors.append(f"LibreOffice: {r.stderr.strip()[:200]}")
    else:
        errors.append("LibreOffice was not found (on a Mac: brew install --cask libreoffice)")
    raise RuntimeError("Could not create the PDF. " + " | ".join(errors))
