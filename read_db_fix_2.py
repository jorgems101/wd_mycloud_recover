#!/usr/bin/env python3
import sqlite3
import shutil
from pathlib import Path
import unicodedata
import re
import csv
from datetime import datetime
from typing import Iterable, Optional

# ================== CONFIGURACIÓN RÁPIDA ==================
DB_PATH    = Path("/Users/jms/projects/fixWD/db/index.db")         # <-- Path, no str
SRC_DIR    = Path("/Volumes/backupWD8TB/recuperacionWD/restsdk/data/files")
DST_DIR    = Path("/Volumes/backupWD8TB/recuperacionWD/output")
TABLE      = "Files"
ID_COL     = "id"            # columna id (para subcarpeta)
NAME_COL   = "name"          # nombre destino (sin extensión)
CONTENT_ID_COL = "contentID" # NOMBRE DEL FICHERO DE ORIGEN (sin o con extensión)
BATCH      = 10_000
PREFIX_LEN = 1               # nº de caracteres del id para formar subcarpeta
GLOB_EXTS  = ["*"]           # ["jpg","png","mp4"] o ["*"] para cualquiera
DRY_RUN    = False
OVERWRITE  = False
REPORT_CSV = f"copy_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
# ===========================================================

# ---- Manejo de textos con codificaciones raras ----
def smart_text_factory(b):
    if b is None:
        return None
    if isinstance(b, bytes):
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", errors="replace")
    return b

def norm(s) -> str:
    """Normaliza unicode y recorta extremos (no elimina espacios intermedios)."""
    return unicodedata.normalize("NFKC", str(s)).strip() if s is not None else ""

def open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    con = sqlite3.connect(uri, uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.text_factory = smart_text_factory
    try:
        con.execute("PRAGMA journal_mode=OFF;")
        con.execute("PRAGMA synchronous=OFF;")
        con.execute("PRAGMA temp_store=MEMORY;")
    except sqlite3.DatabaseError:
        pass
    return con

def stream_rows(con: sqlite3.Connection, sql: str, fetch_size: int = 10_000) -> Iterable[sqlite3.Row]:
    cur = con.cursor()
    cur.execute(sql)
    try:
        while True:
            rows = cur.fetchmany(fetch_size)
            if not rows:
                break
            for r in rows:
                yield r
    finally:
        cur.close()

# ---- Normalización de nombres destino ----
_ILLEGAL_RE = re.compile(r'[\\/:\*\?"<>\|\x00-\x1F]')  # Win/mac/Linux incompatible
def sanitize_name(name: str, max_len: int = 240) -> str:
    if name is None:
        name = ""
    name = unicodedata.normalize("NFKC", str(name)).strip()
    name = _ILLEGAL_RE.sub("_", name)
    if name in {"", ".", ".."}:
        name = "_"
    if len(name) > max_len:
        name = name[:max_len]
    return name

def unique_path(base: Path) -> Path:
    """Si base existe, crea base-1, base-2, ... conservando extensión."""
    if not base.exists():
        return base
    stem, suffix = base.stem, base.suffix
    i = 1
    while True:
        candidate = base.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1

# ---- Helpers de paths ----
def subfolder_for_id(file_id: str) -> Path:
    """Subcarpeta a partir del prefijo del id (en minúsculas)."""
    s = norm(file_id)
    return Path(s[:PREFIX_LEN].lower()) if s else Path("")

def case_insensitive_match(dir_path: Path, filename: str) -> Optional[Path]:
    """
    Busca filename de forma case-insensitive dentro de dir_path.
    Devuelve la primera coincidencia exacta por nombre (ignorando mayúsculas).
    """
    target_lower = filename.lower()
    try:
        for entry in dir_path.iterdir():
            if entry.is_file() and entry.name.lower() == target_lower:
                return entry
    except FileNotFoundError:
        return None
    return None

def find_source_file(src_dir: Path, file_id: str, content_id: str, glob_exts: list[str]) -> Optional[Path]:
    """
    Regla pedida:
      - Carpeta: SRC_DIR / <prefijo(id)>
      - Fichero: <contentID> (con extensión si la trae; si no, buscar <contentID>.*)
    """
    fid = norm(file_id)
    cid = norm(content_id)
    if not fid or not cid:
        return None

    subfolder = src_dir / subfolder_for_id(fid)
    if not subfolder.is_dir():
        return None

    # 1) Si contentID ya trae extensión, probar exacto (case-insensitive también)
    cid_path = subfolder / cid
    if cid_path.suffix:
        if cid_path.is_file():
            return cid_path
        # Buscar case-insensitive
        found = case_insensitive_match(subfolder, cid_path.name)
        if found:
            return found
        return None

    # 2) No trae extensión: probar glob con extensiones conocidas o cualquiera
    candidates = []
    if glob_exts == ["*"]:
        candidates = sorted(subfolder.glob(f"{cid}.*")) + sorted(subfolder.glob(cid))
    else:
        for ext in glob_exts:
            candidates += list(subfolder.glob(f"{cid}.{ext.strip('.')}"))
        if not candidates:
            candidates = sorted(subfolder.glob(f"{cid}.*"))

    if candidates:
        return candidates[0]

    # 3) Último intento: buscar case-insensitive sin conocer extensión
    try:
        for entry in subfolder.iterdir():
            if entry.is_file() and (entry.stem.lower() == cid.lower()):
                return entry
    except FileNotFoundError:
        pass

    return None

def copy_with_name(src: Path, dst_dir: Path, name_no_ext: str, overwrite: bool) -> tuple[bool, str, Path | None]:
    """
    Copia src -> dst_dir / (sanitize(name_no_ext) + src.suffix)
    Maneja colisiones y devuelve (ok, mensaje, ruta_destino_o_None)
    """
    if not src.exists() or not src.is_file():
        return False, f"ERROR: source no existe o no es fichero: {src}", None

    name_no_ext = sanitize_name(name_no_ext)
    # Mantiene la extensión del origen
    target = dst_dir / f"{name_no_ext}{src.suffix}"

    if target.exists():
        if overwrite:
            action = "overwrite"
        else:
            # target = unique_path(target)
            action = "unique"
    else:
        action = "new"

    if DRY_RUN:
        return True, f"DRY_RUN {action}: {src.name} -> {target}", target

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)  # <- COPIA REAL
        return True, f"{action}: {src.name} -> {target}", target
    except Exception as e:
        return False, f"ERROR copying {src} -> {target}: {e}", None

# ---- Reconstruye carpeta destino relativa con la jerarquía (id -> parentID) ----
def get_complete_path(con: sqlite3.Connection, file_id: str) -> Optional[str]:
    """
    Construye la ruta relativa de carpetas desde el nodo hasta la raíz (excluyendo el propio archivo).
    Ej.: 'Album/Vacaciones/2020'
    """
    path_parts = []
    current_id = file_id

    while current_id:
        with con.cursor() as cur:
            cur.execute("SELECT name, parentID FROM Files WHERE id = ? LIMIT 1;", (current_id,))
            row = cur.fetchone()

        if not row:
            break

        name, parent_id = row
        # No añadimos el nombre del registro inicial (el archivo), solo los directorios padres
        if parent_id is None or parent_id == "":
            # Llegamos a raíz
            break

        # Mover hacia arriba agregando el nombre *del padre* cuando toque
        # Para evitar incluir el propio archivo, añadimos nombres una iteración después
        # (i.e., cuando subimos al padre).
        current_id = parent_id
        # Añadir nombre del nodo anterior (si era carpeta). Si era archivo, no pasa nada por añadir: luego
        # al construir el destino se añade el filename y este path queda como carpeta.
        if name:
            path_parts.append(norm(name))

    if path_parts:
        return "/" + "/".join(reversed(path_parts))
    return None

def main():
    # Validaciones de rutas base
    assert SRC_DIR.is_dir(), f"Directorio origen no existe: {SRC_DIR}"
    DST_DIR.mkdir(parents=True, exist_ok=True)

    con = open_sqlite_readonly(DB_PATH)
    sql = f'SELECT {ID_COL} AS _id, {NAME_COL} AS _name, {CONTENT_ID_COL} AS _content_id FROM {TABLE};'

    total = ok = skipped = not_found = errors = 0

    with open(REPORT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["status", "id", "name", "contentID", "src", "dst", "message"])

        for row in stream_rows(con, sql, fetch_size=BATCH):
            total += 1
            file_id   = row["_id"]
            file_name = row["_name"]
            content_id = row["_content_id"]

            # Normalizamos cadenas leídas
            fid = norm(file_id)
            fname = norm(file_name)
            cid = norm(content_id)

            if not cid:
                not_found += 1
                w.writerow(["NOT_FOUND", fid, fname, cid, "", "", "contentID vacío"])
                continue

            # Busca en origen: SUBCARPETA por ID + fichero por CONTENT_ID (con/sin extensión)
            src = find_source_file(SRC_DIR, fid, cid, GLOB_EXTS)
            if src is None:
                not_found += 1
                w.writerow(["NOT_FOUND", fid, fname, cid, "", "", "No source file in id-prefix folder"])
                continue

            # Reconstituye carpeta de destino (relativa) desde jerarquía Files
            rel_folder = get_complete_path(con, fid)
            rel_folder = rel_folder.lstrip("/") if rel_folder else ""  # quitar posible "/" inicial
            dst_folder = DST_DIR / rel_folder

            # Si ya existe el destino exacto y no sobrescribimos -> skip
            sanitized = sanitize_name(fname)
            dst_candidate = dst_folder / f"{sanitized}{src.suffix}"
            if dst_candidate.exists() and not OVERWRITE:
                skipped += 1
                w.writerow(["SKIPPED", fid, fname, cid, str(src), str(dst_candidate), "Destination exists"])
                continue

            success, msg, dst = copy_with_name(src, dst_folder, sanitized, OVERWRITE)
            if success:
                ok += 1
                w.writerow(["OK", fid, fname, cid, str(src), str(dst) if dst else "", msg])
            else:
                errors += 1
                w.writerow(["ERROR", fid, fname, cid, str(src), str(dst) if dst else "", msg])

    con.close()
    print(f"Total: {total} | OK: {ok} | SKIPPED: {skipped} | NOT_FOUND: {not_found} | ERRORS: {errors}")
    print(f"Reporte: {REPORT_CSV}")
    if DRY_RUN:
        print("⚠️ DRY_RUN activo. Pon DRY_RUN=False para ejecutar copias reales.")

if __name__ == "__main__":
    main()
