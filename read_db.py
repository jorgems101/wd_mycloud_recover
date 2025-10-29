#!/usr/bin/env python3
import sqlite3
from typing import Iterable, Tuple, Any, Optional

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

def open_sqlite_readonly(path: str) -> sqlite3.Connection:
    """
    Abre una BD SQLite en modo solo lectura usando URI.
    Ejemplo de path:
      /media/WD/mi_base.sqlite
    """
    uri = f"file:{path}?mode=ro"
    # check_same_thread=False por si más tarde quieres leer desde hilos
    con = sqlite3.connect(uri, uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.text_factory = smart_text_factory
    # Ajustes que ayudan en lectura (algunos pueden ser ignorados en read-only)
    try:
        con.execute("PRAGMA journal_mode=OFF;")
        con.execute("PRAGMA synchronous=OFF;")
        con.execute("PRAGMA temp_store=MEMORY;")
        con.execute("PRAGMA cache_size=-200000;")  # ~200 MB de caché en RAM (negativo = KiB)
    except sqlite3.DatabaseError:
        # Si alguno no aplica en read-only, ignoramos
        pass
    # Mejoras de rendimiento del driver
    con.row_factory = sqlite3.Row  # acceso por nombre de columna si quieres
    return con

def list_tables(con: sqlite3.Connection) -> list[str]:
    """
    Devuelve las tablas visibles en la BD (omite internas).
    """
    sql = """
    SELECT name
    FROM sqlite_schema
    WHERE type='table' AND name NOT LIKE 'sqlite_%'
    ORDER BY name;
    """
    return [r[0] for r in con.execute(sql).fetchall()]

def stream_query(con: sqlite3.Connection, sql: str, params: Optional[Tuple[Any, ...]] = None,
                 fetch_size: int = 10_000) -> Iterable[Tuple[Any, ...]]:
    """
    Ejecuta una consulta grande y devuelve filas en lotes.
    - No carga todo en memoria.
    - Devuelve primero una tupla con los nombres de columnas.
    """
    cur = con.cursor()
    cur.execute(sql, params or ())
    # Cabeceras
    colnames = tuple(d[0] for d in cur.description)
    yield colnames

    while True:
        rows = cur.fetchmany(fetch_size)
        if not rows:
            break
        for row in rows:
            # row puede ser sqlite3.Row si usas row_factory; conviértelo a tupla
            yield tuple(row)

    cur.close()


def get_mount_point(con: sqlite3.Connection, volume_id: str) -> Optional[str]:
    """
    Obtiene el mountPoint de un volumen dado su id.
    Devuelve None si no se encuentra.
    """
    sql = "SELECT mountPoint FROM Volumes WHERE id = ? LIMIT 1;"
    cur = con.cursor()
    cur.execute(sql, (volume_id,))
    row = cur.fetchone()
    cur.close()
    if row:
        return row[0]
    return None
def get_complete_path(con: sqlite3.Connection, file_id: str) -> Optional[str]:
    """
    Obtiene la ruta completa de un archivo dado su id.
    Devuelve None si no se encuentra.
    """
    path_parts = []
    current_id = file_id

    while current_id:
        sql = "SELECT name, parentID FROM Files WHERE id = ? LIMIT 1;"
        cur = con.cursor()
        cur.execute(sql, (current_id,))
        row = cur.fetchone()
        cur.close()
        if row:
            name, parent_id = row
            # if parent_id is None or parent_id == "":
            #     # Hemos llegado a la raíz
            #     break
            path_parts.append(name)
            current_id = parent_id
        else:
            break

    if path_parts:
        return '/' + '/'.join(reversed(path_parts))
    return None

# def copy_with_name(src: Path, dst_dir: Path, name_no_ext: str, overwrite: bool) -> tuple[bool, str, Path | None]:
#     """
#     Copia src -> dst_dir / (sanitize(name_no_ext) + src.suffix)
#     Maneja colisiones y devuelve (ok, mensaje, ruta_destino_o_None)
#     """
#     name_no_ext = sanitize_name(name_no_ext)
#     suffix = src.suffix  # preserva extensión del origen
#     target = dst_dir / f"{name_no_ext}{suffix}"
#     if target.exists():
#         if overwrite:
#             action = "overwrite"
#         else:
#             target = unique_path(target)
#             action = "unique"
#     else:
#         action = "new"

#     if DRY_RUN:
#         return True, f"DRY_RUN {action}: {src.name} -> {target.name}", target

#     try:
#         target.parent.mkdir(parents=True, exist_ok=True)
#         shutil.copy2(src, target)  # conserva metadatos
#         return True, f"{action}: {src.name} -> {target.name}", target
#     except Exception as e:
#         return False, f"ERROR copying {src} -> {target}: {e}", None


if __name__ == "__main__":
    # ==== USO RÁPIDO ====
    # 1) Cambia la ruta a tu archivo .sqlite / .db
    DB_PATH = "/Users/jms/projects/fixWD/db/index.db"

    con = open_sqlite_readonly(DB_PATH)

    # 2) Listar tablas
    tablas = list_tables(con)
    if not tablas:
        print("No se encontraron tablas.")
    else:
        print("Tablas encontradas:")
        for t in tablas:
            print(f"  - {t}")

    table_to_inspect = "Files"

    # 3) (Opcional) Prueba de lectura por lotes sobre la primera tabla
    #    Descomenta si quieres ver 2 lotes de 5 filas:
    if tablas:
        print("\nLeyendo 10 filas (2 lotes de 5) de la primera tabla:")
        for table in tablas:
            print(f"  - {table}")
            gen = stream_query(con, f"SELECT * FROM {table} LIMIT 10", fetch_size=5)
            for i, row in enumerate(gen):
                if i == 0:
                    print(" | ".join(row))  # cabeceras
                    print("\n")
                else:
                    print(" | ".join(str(x) if x is not None else "" for x in row))
            print("\n")
    # con.close()

    volumes_table = "Volumes"
    files_table = "Files"

    if tablas:
        print("\nLeyendo 10 filas (2 lotes de 5) de la primera tabla:")
        gen = stream_query(con, f"SELECT * FROM {files_table} LIMIT 30", fetch_size=5)
        columns = ["id","name", "parentID", "contentID","previewSourceContentID", "autoID", "mimeType"]
        index_filter = []
        for i, row in enumerate(gen):
            if i == 0:
                # print(" | ".join(row))  # cabeceras
                # print(f"row[{i}]={row}")
                print("\n ".join(row))
                index=0
                dict_row = dict()
                for col in row:
                    dict_row[col] = index
                    index += 1
                print(f"Columns: {dict_row}")
                index_filter = [dict_row[col] for col in columns if col in dict_row]
                print(f"Index filter: {index_filter}")
                for col in columns:
                    if col in dict_row:
                        print(f"Column '{col}' has index {dict_row[col]}")
                print("\n")
            else:
                # print(" | ".join(str(x) if x is not None else "" for x in row))
                print(" | ".join(str(row[index]) if row[index] is not None else "" for index in index_filter))
                # print(f"Row {i}: " + ", ".join(str(row[col]) if row[col] is not None else "" for col in columns))
                # print(f"Row {i}:{row[0]}")
                print(" | ".join(str(r) if r is not None else "" for r in row))
                # print(get_mount_point(con, row[0]))
                # print(get_mount_point(con, row[1]))
                # print(get_mount_point(con, row[2]))
                print(get_complete_path(con, row[0]))
        print("\n")
        print(get_complete_path(con, "2levlcp3i6uyh6hpmafm7aco"))
        # gen = stream_query(con, f"SELECT * FROM {volumes_table} LIMIT 100", fetch_size=5)
        # columns = ["id","mountPoint"]
        # index_filter = []
        # for i, row in enumerate(gen):
        #     if i == 0:
        #         # print(" | ".join(row))  # cabeceras
        #         # print(f"row[{i}]={row}")
        #         print("\n ".join(row))
        #         index=0
        #         dict_row = dict()
        #         for col in row:
        #             dict_row[col] = index
        #             index += 1
        #         print(f"Columns: {dict_row}")
        #         index_filter = [dict_row[col] for col in columns if col in dict_row]
        #         print(f"Index filter: {index_filter}")
        #         for col in columns:
        #             if col in dict_row:
        #                 print(f"Column '{col}' has index {dict_row[col]}")
        #         print("\n")
        #     else:
        #         # print(" | ".join(str(x) if x is not None else "" for x in row))
        #         print(" | ".join(str(row[index]) if row[index] is not None else "" for index in index_filter))
        
        # row = get_mount_point(con, "y6d722sxmnsfllv5yy6gzy6r")
        # print("\n")
        # print(row)
        print("\n")
    con.close()