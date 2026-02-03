# -*- coding: utf-8 -*-
"""
ml_facturator_ventas_ops.py — Alta silenciosa de compra/venta + remito temporal, reservas y registros auxiliares

Archivo COMPLETO y listo para reemplazar.

Cambios principales (además de lo que ya hacía):
- NUEVO: alta_compra_silenciosa_6711_mercado_envios(proveedor_code="034", amount, deposito_codigos="1")
  - Genera una compra silenciosa del SKU 6711 al proveedor FIJO indicado (por defecto 034).
  - No intenta resolver por nombre: usa el código directamente (normalizado zfill(3)).
  - Crea it_comp del artículo 6711 con costo=amount y precio=0 (es costo-only).
  - Inserta 1 código en codigos para 6711.

- NUEVO (febrero 2026): alta_compra_silenciosa_6756_bonificacion_ml(proveedor_code="034", amount, deposito_codigos="1")
  - Genera una compra silenciosa del SKU 6756 (Bonificación MercadoLibre) al proveedor FIJO (por defecto 034).
  - Similar a 6711 pero para la bonificación de envío que ML otorga en el caso de "envío compartido".
  - Crea it_comp del artículo 6756 con costo=0 y precio=0 (es bonificación, no costo real).
  - Inserta 1 código en codigos para 6756.
  - Se usa cuando shipping_cost_seller > nuestro_costo_envio (diferencia = bonificación ML).

- En alta_venta_silenciosa_directa:
  - Si un item trae campo "costo" (>0), ese costo tiene prioridad absoluta sobre resolver_costo_para_sku,
    aunque la venta/precio sea 0. Esto permite SKU 6711 costo-only.
  - Si la venta es MERCADO ENVIOS, al insertar registros en movimientos_mp se establecerá TIPO="MERCADOENVIO"
    (si la columna TIPO existe en la tabla movimientos_mp se insertará; si no, la columna será creada por _ensure_movimientos_mp).

Se mantiene:
- Compras silenciosas 6696 por motoquero (resolviendo proveedor).
- Contadores paramet separados ventas/compras.
- Reservas de códigos, deposito='' al asignar remito_ven.
- movimientos_mp (PACK, TIPO) y envios_flex.
"""
import os
import sqlite3
import datetime as dt
from typing import Dict, List, Any, Optional, Iterable, Mapping, Tuple

DB_PATH = r"C:\!GESTION2026\gestion.sqlite3"

from ml_facturador_remito_temporal_codigos_ops import (
    tomar_codigos_para_remito,
    liberar_codigos_de_remito,
)

from ml_facturator_cost_ops import (
    rellenar_costos_en_temp,
    resolver_costo_para_sku,
    costo_para_it_vent,
)

# ---------------- compat: tomar códigos considerando remito_ven vacío o "0" ----------------
def _tomar_codigos_para_remito_compat(con: sqlite3.Connection,
                                     articulo: str,
                                     cantidad: int,
                                     remito: int,
                                     deposito: int = 1) -> List[str]:
    """
    Reserva 'cantidad' códigos de CODIGOS para un artículo, tratando remito_ven NULL / '' / '0' como LIBRE.
    Es un blindeo para casos donde remito_ven quedó en 0.
    Devuelve la lista de códigos reservados (len puede ser < cantidad si no alcanza stock).
    """
    try:
        cantidad = int(cantidad or 0)
    except Exception:
        cantidad = 0
    if cantidad <= 0:
        return []

    art = _norm_sku(articulo)
    dep_i = int(deposito or 1)
    dep_s = str(dep_i)

    cur = con.cursor()
    cols = _table_cols(cur, "codigos")
    if not cols:
        return []

    col_codigo = _pick(cols, ["codigo"])
    col_art    = _pick(cols, ["articulo"])
    col_dep    = _pick(cols, ["deposito"])
    col_remven = "remito_ven" if "remito_ven" in cols else None
    if not col_remven:
        return []

    # Seleccionar códigos libres (remito_ven NULL/''/'0') en el depósito indicado
    cur.execute(f"""
        SELECT {col_codigo}
          FROM codigos
         WHERE TRIM({col_art})=?
           AND ({col_dep}=? OR TRIM({col_dep})=?)
           AND ({col_remven} IS NULL OR TRIM(CAST({col_remven} AS TEXT)) IN ('', '0'))
         ORDER BY {col_codigo}
         LIMIT ?
    """, (art, dep_i, dep_s, cantidad))
    rows = cur.fetchall() or []
    cods = [str(r[0]) for r in rows if r and r[0] is not None]

    if not cods:
        return []

    # Reservar: remito_ven = remito, deposito = '' (igual que el flujo actual)
    cur.executemany(
        f"UPDATE codigos SET {col_remven}=?, {col_dep}='' WHERE {col_codigo}=?",
        [(int(remito), c) for c in cods]
    )
    return cods


# ---------------- utilidades generales ----------------
def _ensure_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return con


def _has_table(cur: sqlite3.Cursor, name: str) -> bool:
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
        return cur.fetchone() is not None
    except Exception:
        return False


def _table_cols(cur: sqlite3.Cursor, name: str) -> List[str]:
    try:
        cur.execute(f"PRAGMA table_info({name})")
        return [r[1] for r in cur.fetchall()]
    except Exception:
        return []


def _pick(cols: List[str], candidates: List[str]) -> str:
    low = {c.lower(): c for c in cols}
    for c in candidates:
        if c in cols:
            return c
        if c.lower() in low:
            return low[c.lower()]
    return cols[0] if cols else (candidates[0] if candidates else "")


def _to_float(x) -> float:
    """Parsea números monetarios en formato AR (1.234,56) y US (1,234.56 / 1234.56).
    Evita el bug de interpretar '.' decimal como separador de miles.
    """
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        try:
            return float(x)
        except Exception:
            return 0.0
    try:
        s = str(x).strip()
    except Exception:
        return 0.0
    if not s:
        return 0.0
    s = s.replace("$", "").replace("ARS", "").replace(" ", "")
    has_comma = "," in s
    has_dot = "." in s
    if has_comma and has_dot:
        # Decide decimal por el último separador
        if s.rfind(",") > s.rfind("."):
            # AR: 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:
            # US: 1,234.56
            s = s.replace(",", "")
    elif has_comma and not has_dot:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            s = parts[0] + "." + parts[1]
        else:
            s = s.replace(",", "")
    elif has_dot and not has_comma:
        parts = s.split(".")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            # dot decimal
            pass
        else:
            # dot miles
            s = s.replace(".", "")
    try:
        return float(s)
    except Exception:
        return 0.0


def _norm_sku(sku: Any) -> str:
    raw = str(sku or "").strip()
    if not raw:
        return ""
    return raw.zfill(4) if raw.isdigit() else raw


def _norm_proveedor_code_3(prov_code_int: int) -> str:
    try:
        return str(max(0, int(prov_code_int))).zfill(3)
    except Exception:
        return "000"


# ---------------- manejo de contadores paramet ----------------
def _ensure_paramet_counters(cur: sqlite3.Cursor):
    cur.execute("PRAGMA table_info(paramet)")
    cols = [r[1].lower() for r in cur.fetchall()]
    if "compras" not in cols:
        cur.execute("SELECT COUNT(*) FROM paramet")
        total = (cur.fetchone() or [0])[0]
        if not total:
            cur.execute("CREATE TABLE IF NOT EXISTS paramet (ventas INTEGER, compras INTEGER)")
            cur.execute("INSERT INTO paramet (ventas, compras) VALUES (?, ?)", (0, 0))
        else:
            try:
                cur.execute("ALTER TABLE paramet ADD COLUMN compras INTEGER")
            except Exception:
                pass
            try:
                cur.execute("SELECT ventas FROM paramet LIMIT 1")
                row = cur.fetchone()
                ventas_val = int(row[0] or 0) if row else 0
                cur.execute("UPDATE paramet SET compras = ?", (ventas_val,))
            except Exception:
                cur.execute("UPDATE paramet SET compras = 0")


def _reservar_remito_venta(con: sqlite3.Connection) -> int:
    cur = con.cursor()
    _ensure_paramet_counters(cur)
    cur.execute("SELECT ventas FROM paramet LIMIT 1")
    row = cur.fetchone()
    if not row:
        raise RuntimeError("Tabla 'paramet' vacía — no puedo reservar remito de venta.")
    remito_actual = int(row[0]) if row[0] is not None else 0
    nuevo_remito = remito_actual + 1
    cur.execute("UPDATE paramet SET ventas = ?", (nuevo_remito,))
    con.commit()
    return nuevo_remito


def _reservar_remito_compra(con: sqlite3.Connection) -> int:
    cur = con.cursor()
    _ensure_paramet_counters(cur)
    cur.execute("SELECT compras FROM paramet LIMIT 1")
    row = cur.fetchone()
    if not row:
        raise RuntimeError("Tabla 'paramet' vacía — no puedo reservar remito de compra.")
    compra_actual = int(row[0]) if row[0] is not None else 0
    nueva_compra = compra_actual + 1
    cur.execute("UPDATE paramet SET compras = ?", (nueva_compra,))
    con.commit()
    return nueva_compra


# ---------------- tabla proveedores: resolución de código ----------------
def _resolve_proveedor_code(cur: sqlite3.Cursor, vendor_name_or_code: str) -> int:
    raw = str(vendor_name_or_code or "").strip()
    if not raw:
        return 0

    if raw.isdigit():
        try:
            return int(raw)
        except Exception:
            return 0

    up = raw.upper().strip()

    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
    except Exception:
        tables = []

    prov_tab = None
    for t in ("proveedores", "PROVEEDORES", "proveedor", "PROVEEDOR", "proveed"):
        if t in tables:
            prov_tab = t
            break
    if not prov_tab:
        return 0

    try:
        cur.execute(f"PRAGMA table_info('{prov_tab}')")
        cols = [r[1] for r in cur.fetchall()]
        name_col = _pick(cols, ["nombre", "razon_social", "razon", "vendor", "proveedor", "name"])
        code_col = _pick(cols, ["codigo", "cod", "id", "code"])
    except Exception:
        return 0

    try:
        cur.execute(f'SELECT "{code_col}" FROM "{prov_tab}" WHERE TRIM(UPPER("{name_col}")) = ?', (up,))
        row = cur.fetchone()
        if row and row[0] is not None:
            return int(str(row[0]).strip())
    except Exception:
        pass

    try:
        cur.execute(f'SELECT "{code_col}" FROM "{prov_tab}" WHERE UPPER("{name_col}") LIKE ?', (f"%{up}%",))
        row = cur.fetchone()
        if row and row[0] is not None:
            return int(str(row[0]).strip())
    except Exception:
        pass

    for alias in ("PATO", "JHONATAN", "NSA"):
        if alias in up:
            try:
                cur.execute(f'SELECT "{code_col}" FROM "{prov_tab}" WHERE TRIM(UPPER("{name_col}")) = ?', (alias,))
                row = cur.fetchone()
                if row and row[0] is not None:
                    return int(str(row[0]).strip())
                cur.execute(f'SELECT "{code_col}" FROM "{prov_tab}" WHERE UPPER("{name_col}") LIKE ?', (f"%{alias}%",))
                row = cur.fetchone()
                if row and row[0] is not None:
                    return int(str(row[0]).strip())
            except Exception:
                pass

    return 0


# ---------------- manejo de remito temporal ----------------
def _crear_tabla_temporal_remito(con: sqlite3.Connection, remito: int) -> str:
    if not isinstance(remito, int) or remito <= 0:
        raise ValueError("Remito inválido para tabla temporal.")
    tname = f"TEMP_REMITO_VEN_{remito}"
    cur = con.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {tname}")
    cur.execute(f"""
        CREATE TABLE {tname}(
            SKU    TEXT,
            CODIGO TEXT,
            remito INTEGER,
            costo  REAL,
            venta  REAL
        )
    """)
    con.commit()
    return tname


def _insert_items_temporales(con: sqlite3.Connection, tname: str, remito: int, items: Iterable[Mapping], envio_cost_value: float = 0.0):
    """
    Inserta filas en la tabla TEMP:
      - costo:
          1) si item trae 'costo' (>0) => usarlo (permite 6711 costo-only)
          2) si it trae costo (>0) => usarlo
          3) si no, inferir resolver_costo_para_sku
      - venta: precio unitario visual del detalle (puede ser 0)
      - SKU normalizado.
    """
    rows = []
    for it in items:
        sku_in = str(it.get("sku") or it.get("SKU") or "").strip()
        sku = _norm_sku(sku_in)
        cod = str(it.get("codigo") or it.get("CODIGO") or sku).strip()

        # costo explícito (prioridad)
        costo_item = _to_float(it.get("costo") if it.get("costo") is not None else 0.0)
        if costo_item > 0:
            costo_val = float(costo_item)
        else:
            costo_tr = _to_float(it.get("costo") or 0.0)
            costo_val = float(costo_tr) if costo_tr > 0 else float(resolver_costo_para_sku(con, sku))

        qty = int(it.get("quantity") or 0)
        line_total = it.get("total") if it.get("total") is not None else it.get("subtotal")
        line_total_f = _to_float(line_total or 0.0)
        if qty and line_total_f > 0:
            venta_val = float(line_total_f / qty)
        else:
            venta_val = _to_float(it.get("price") or 0.0)

        rows.append((sku, cod, remito, costo_val, venta_val))

    if rows:
        cur = con.cursor()
        cur.executemany(
            f'INSERT INTO {tname}(SKU, CODIGO, remito, costo, venta) VALUES (?,?,?,?,?)',
            rows
        )
        con.commit()


# ---------------- helpers de codigos ----------------
def _build_codigo_for_compra(articulo: str, remito_compra_num: int) -> str:
    art4 = _norm_sku(articulo)
    rem5 = str(int(remito_compra_num)).zfill(5)
    return f"{art4}{rem5}0001"


def _insert_codigo_for_compra(cur: sqlite3.Cursor, next_nc: int, deposito: str, articulo: str, fecha: str, hora: str, usu: str = "AUTO") -> str:
    cols = _table_cols(cur, "codigos")
    col_remito    = _pick(cols, ["remito"])
    col_articulo  = _pick(cols, ["articulo"])
    col_codigo    = _pick(cols, ["codigo"])
    col_deposito  = _pick(cols, ["deposito"])
    col_mdate     = _pick(cols, ["mdate"])
    col_mhora     = _pick(cols, ["mhora"])
    col_musu      = _pick(cols, ["musu"])
    col_control   = _pick(cols, ["control"])
    col_leido     = _pick(cols, ["leido"])
    col_remito_ven= "remito_ven" if "remito_ven" in cols else None

    articulo_norm = _norm_sku(articulo)
    codigo_gen = _build_codigo_for_compra(articulo_norm, next_nc)

    insert_cols = []
    insert_vals = []

    def add(cname, value):
        if cname and cname in cols:
            insert_cols.append(cname)
            insert_vals.append(value)

    add(col_remito, int(next_nc))
    add(col_articulo, str(articulo_norm))
    add(col_codigo, str(codigo_gen))
    add(col_deposito, str(deposito))
    add(col_mdate, str(fecha))
    add(col_mhora, str(hora))
    add(col_musu, str(usu))
    add(col_control, 0)
    add(col_leido, 0)
    if col_remito_ven:
        insert_cols.append(col_remito_ven)
        insert_vals.append(None)

    if not insert_cols:
        raise RuntimeError("No se pudieron mapear columnas de 'codigos' para insertar el código de compra.")

    cols_sql = ",".join([f'"{c}"' for c in insert_cols])
    qs_sql = ",".join(["?"] * len(insert_vals))
    cur.execute(f"INSERT INTO codigos ({cols_sql}) VALUES ({qs_sql})", tuple(insert_vals))
    return codigo_gen


# ---------------- tablas auxiliares ----------------
def _ensure_movimientos_mp(cur: sqlite3.Cursor):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS movimientos_mp (
            Orden         TEXT,
            movimiento    TEXT,
            fecha         TEXT,
            importe       REAL,
            remito_venta  INTEGER,
            usuario_ml    TEXT
        )
    """)
    try:
        cur.execute("PRAGMA table_info(movimientos_mp)")
        cols = [r[1] for r in cur.fetchall()]
        if "PACK" not in cols and "pack" not in cols:
            try:
                cur.execute("ALTER TABLE movimientos_mp ADD COLUMN PACK TEXT")
            except Exception:
                pass
        # Asegurar columna TIPO para marcar MERCADOENVIO si corresponde
        if "TIPO" not in cols and "tipo" not in cols:
            try:
                cur.execute("ALTER TABLE movimientos_mp ADD COLUMN TIPO TEXT")
            except Exception:
                pass
    except Exception:
        pass


def _ensure_envios_flex(cur: sqlite3.Cursor):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS envios_flex (
            remito           INTEGER,
            fecha            TEXT,
            motoquero        TEXT,
            costo_de_envio   REAL,
            cobro_de_envio   REAL,
            ganancia         REAL
        )
    """)


# ---------------- compra silenciosa 6696 ----------------
def alta_compra_silenciosa_6696(vendor_name: Optional[str], amount: float, deposito_codigos: str = "1") -> Dict[str, Any]:
    prov_raw = str(vendor_name or "").strip()
    prov_up = prov_raw.upper()
    try:
        con = _ensure_db()
    except Exception as ex:
        return {"ok": False, "error": f"No se pudo abrir DB: {ex}"}
    cur = con.cursor()

    if not (_has_table(cur, "compras") and _has_table(cur, "it_comp") and _has_table(cur, "codigos") and _has_table(cur, "paramet")):
        try:
            con.close()
        except Exception:
            pass
        return {"ok": False, "error": "Faltan tablas 'compras'/'it_comp'/'codigos'/'paramet'."}

    try:
        next_nc = _reservar_remito_compra(con)

        fecha_solo = dt.datetime.now().strftime("%Y-%m-%d")
        hora_solo = dt.datetime.now().strftime("%H:%M:%S")

        prov_code_int = _resolve_proveedor_code(cur, prov_up)
        prov_code_str = _norm_proveedor_code_3(prov_code_int)

        cols_c = _table_cols(cur, "compras")
        col_num = _pick(cols_c, ["compra", "nro", "numero", "id", "mr", "remito"])
        col_pro = _pick(cols_c, ["proveedor", "prov", "vendor", "cod_prov", "codigo_proveedor"])
        col_tot = _pick(cols_c, ["total", "tot", "importe"])
        col_fec = _pick(cols_c, ["fecha", "fech", "fch", "dia"])
        cur.execute(
            f'INSERT INTO compras ("{col_num}","{col_pro}","{col_tot}","{col_fec}") VALUES (?,?,?,?)',
            (int(next_nc), str(prov_code_str), float(amount or 0.0), fecha_solo)
        )

        cols_ic = _table_cols(cur, "it_comp")
        rem_c = _pick(cols_ic, ["remito", "mr", "nroremito", "id_remito"])
        art_c = _pick(cols_ic, ["articulo", "codigo", "sku", "cod_art"])
        cant_c = _pick(cols_ic, ["cant", "cantidad"])
        costo_c = _pick(cols_ic, ["costo", "cost", "precio_costo", "pcosto"])
        precio_c = _pick(cols_ic, ["precio", "precio_unit", "punit", "importe"])
        cur.execute(
            f'INSERT INTO it_comp ("{rem_c}","{art_c}","{cant_c}","{costo_c}","{precio_c}") VALUES (?,?,?,?,?)',
            (int(next_nc), _norm_sku("6696"), 1, float(amount or 0.0), float(amount or 0.0))
        )

        codigo_envio = _insert_codigo_for_compra(cur, next_nc, deposito_codigos, _norm_sku("6696"), fecha_solo, hora_solo, "AUTO-6696")

        con.commit()
        try:
            con.close()
        except Exception:
            pass
        return {"ok": True, "compra": int(next_nc), "proveedor": str(prov_code_str), "codigo_envio": codigo_envio}
    except Exception as ex:
        try:
            con.rollback()
        except Exception:
            pass
        try:
            con.close()
        except Exception:
            pass
        return {"ok": False, "error": f"Compra silenciosa 6696 falló: {ex}"}


# ---------------- compra silenciosa 6711 (MERCADO ENVIOS) ----------------
def alta_compra_silenciosa_6711_mercado_envios(proveedor_code: str = "034",
                                               amount: float = 0.0,
                                               deposito_codigos: str = "1") -> Dict[str, Any]:
    """
    Compra silenciosa del SKU 6711 al proveedor FIJO (default 034).
    - Usa paramet.compras.
    - compras.proveedor = proveedor_code zfill(3)
    - it_comp: articulo=6711, cantidad=1, costo=amount, precio=0
    - Crea 1 código en codigos para 6711.
    """
    try:
        prov_code_str = str(int(str(proveedor_code).strip())).zfill(3)
    except Exception:
        prov_code_str = "000"

    try:
        con = _ensure_db()
    except Exception as ex:
        return {"ok": False, "error": f"No se pudo abrir DB: {ex}"}
    cur = con.cursor()

    if not (_has_table(cur, "compras") and _has_table(cur, "it_comp") and _has_table(cur, "codigos") and _has_table(cur, "paramet")):
        try:
            con.close()
        except Exception:
            pass
        return {"ok": False, "error": "Faltan tablas 'compras'/'it_comp'/'codigos'/'paramet'."}

    try:
        next_nc = _reservar_remito_compra(con)

        fecha_solo = dt.datetime.now().strftime("%Y-%m-%d")
        hora_solo = dt.datetime.now().strftime("%H:%M:%S")

        cols_c = _table_cols(cur, "compras")
        col_num = _pick(cols_c, ["compra", "nro", "numero", "id", "mr", "remito"])
        col_pro = _pick(cols_c, ["proveedor", "prov", "vendor", "cod_prov", "codigo_proveedor"])
        col_tot = _pick(cols_c, ["total", "tot", "importe"])
        col_fec = _pick(cols_c, ["fecha", "fech", "fch", "dia"])
        cur.execute(
            f'INSERT INTO compras ("{col_num}","{col_pro}","{col_tot}","{col_fec}") VALUES (?,?,?,?)',
            (int(next_nc), str(prov_code_str), float(amount or 0.0), fecha_solo)
        )

        cols_ic = _table_cols(cur, "it_comp")
        rem_c = _pick(cols_ic, ["remito", "mr", "nroremito", "id_remito"])
        art_c = _pick(cols_ic, ["articulo", "codigo", "sku", "cod_art"])
        cant_c = _pick(cols_ic, ["cant", "cantidad"])
        costo_c = _pick(cols_ic, ["costo", "cost", "precio_costo", "pcosto"])
        precio_c = _pick(cols_ic, ["precio", "precio_unit", "punit", "importe"])
        cur.execute(
            f'INSERT INTO it_comp ("{rem_c}","{art_c}","{cant_c}","{costo_c}","{precio_c}") VALUES (?,?,?,?,?)',
            (int(next_nc), _norm_sku("6711"), 1, float(amount or 0.0), 0.0)
        )

        codigo_6711 = _insert_codigo_for_compra(cur, next_nc, deposito_codigos, _norm_sku("6711"), fecha_solo, hora_solo, "AUTO-6711-ME")

        con.commit()
        try:
            con.close()
        except Exception:
            pass
        return {"ok": True, "compra": int(next_nc), "proveedor": str(prov_code_str), "codigo_envio": codigo_6711}
    except Exception as ex:
        try:
            con.rollback()
        except Exception:
            pass
        try:
            con.close()
        except Exception:
            pass
        return {"ok": False, "error": f"Compra silenciosa 6711 (ME) falló: {ex}"}


# ---------------- compra silenciosa 6756 (BONIFICACION MERCADO ENVIOS) ----------------
def alta_compra_silenciosa_6756_bonificacion_ml(proveedor_code: str = "034",
                                                 amount: float = 0.0,
                                                 deposito_codigos: str = "1") -> Dict[str, Any]:
    """
    Compra silenciosa del SKU 6756 (Bonificación MercadoLibre) al proveedor FIJO (default 034).
    Similar a 6711 pero para la bonificación de envío que ML otorga.
    - Usa paramet.compras.
    - compras.proveedor = proveedor_code zfill(3)
    - it_comp: articulo=6756, cantidad=1, costo=0, precio=0
    - Crea 1 código en codigos para 6756.
    - La diferencia con 6711 es que el COSTO es 0 (es una bonificación, no un costo real).
    """
    try:
        prov_code_str = str(int(str(proveedor_code).strip())).zfill(3)
    except Exception:
        prov_code_str = "000"

    try:
        con = _ensure_db()
    except Exception as ex:
        return {"ok": False, "error": f"No se pudo abrir DB: {ex}"}
    cur = con.cursor()

    if not (_has_table(cur, "compras") and _has_table(cur, "it_comp") and _has_table(cur, "codigos") and _has_table(cur, "paramet")):
        try:
            con.close()
        except Exception:
            pass
        return {"ok": False, "error": "Faltan tablas 'compras'/'it_comp'/'codigos'/'paramet'."}

    try:
        next_nc = _reservar_remito_compra(con)

        fecha_solo = dt.datetime.now().strftime("%Y-%m-%d")
        hora_solo = dt.datetime.now().strftime("%H:%M:%S")

        cols_c = _table_cols(cur, "compras")
        col_num = _pick(cols_c, ["compra", "nro", "numero", "id", "mr", "remito"])
        col_pro = _pick(cols_c, ["proveedor", "prov", "vendor", "cod_prov", "codigo_proveedor"])
        col_tot = _pick(cols_c, ["total", "tot", "importe"])
        col_fec = _pick(cols_c, ["fecha", "fech", "fch", "dia"])
        cur.execute(
            f'INSERT INTO compras ("{col_num}","{col_pro}","{col_tot}","{col_fec}") VALUES (?,?,?,?)',
            (int(next_nc), str(prov_code_str), 0.0, fecha_solo)
        )

        cols_ic = _table_cols(cur, "it_comp")
        rem_c = _pick(cols_ic, ["remito", "mr", "nroremito", "id_remito"])
        art_c = _pick(cols_ic, ["articulo", "codigo", "sku", "cod_art"])
        cant_c = _pick(cols_ic, ["cant", "cantidad"])
        costo_c = _pick(cols_ic, ["costo", "cost", "precio_costo", "pcosto"])
        precio_c = _pick(cols_ic, ["precio", "precio_unit", "punit", "importe"])
        cur.execute(
            f'INSERT INTO it_comp ("{rem_c}","{art_c}","{cant_c}","{costo_c}","{precio_c}") VALUES (?,?,?,?,?)',
            (int(next_nc), _norm_sku("6756"), 1, 0.0, 0.0)
        )

        codigo_6756 = _insert_codigo_for_compra(cur, next_nc, deposito_codigos, _norm_sku("6756"), fecha_solo, hora_solo, "AUTO-6756-BONIF-ML")

        con.commit()
        try:
            con.close()
        except Exception:
            pass
        return {"ok": True, "compra": int(next_nc), "proveedor": str(prov_code_str), "codigo_envio": codigo_6756}
    except Exception as ex:
        try:
            con.rollback()
        except Exception:
            pass
        try:
            con.close()
        except Exception:
            pass
        return {"ok": False, "error": f"Compra silenciosa 6756 (BONIF ML) falló: {ex}"}


# ---------------- helpers de reserva exacta para 6696 ----------------
def _reservar_codigo_envio_exacto(con: sqlite3.Connection,
                                  remito_mr: int,
                                  compra_nc: Optional[int],
                                  codigo_envio: Optional[str]) -> Tuple[bool, str]:
    try:
        cur = con.cursor()
        cols = _table_cols(cur, "codigos")
        col_remito = _pick(cols, ["remito"])
        col_art = _pick(cols, ["articulo", "codigo", "sku", "cod_art"])
        col_codigo = _pick(cols, ["codigo"])
        col_dep = _pick(cols, ["deposito"])
        col_rem_ven = "remito_ven" if "remito_ven" in cols else None

        if codigo_envio and col_rem_ven:
            cur.execute(f"""
                UPDATE codigos
                   SET {col_rem_ven}=?, {col_dep}=''
                 WHERE {col_codigo}=?
                   AND TRIM({col_art})=?
                   AND ({col_rem_ven} IS NULL OR TRIM(CAST({col_rem_ven} AS TEXT)) IN ('', '0'))
            """, (int(remito_mr), str(codigo_envio), _norm_sku("6696")))
            if cur.rowcount and cur.rowcount > 0:
                con.commit()
                return True, "Reservado código 6696 exacto (deposito='')."

        if compra_nc is not None and col_rem_ven:
            cur.execute(f"""
                UPDATE codigos
                   SET {col_rem_ven}=?, {col_dep}=''
                 WHERE {col_remito}=?
                   AND TRIM({col_art})=?
                   AND ({col_rem_ven} IS NULL OR TRIM(CAST({col_rem_ven} AS TEXT)) IN ('', '0'))
            """, (int(remito_mr), int(compra_nc), _norm_sku("6696")))
            if cur.rowcount and cur.rowcount > 0:
                con.commit()
                return True, "Reservado código 6696 de compra silenciosa (deposito='')."

        return False, "No se pudo reservar código exacto 6696."
    except Exception as ex:
        return False, f"Error al reservar código 6696 exacto: {ex}"


# ---------------- alta de venta silenciosa ----------------
def alta_venta_silenciosa_directa(pedido: Dict[str, Any],
                                  vendor_name: Optional[str] = None,
                                  envio_cost_value: float = 0.0,
                                  es_flex: Optional[bool] = None,
                                  compra_nc_for_envio: Optional[int] = None,
                                  codigo_envio_creado: Optional[str] = None,
                                  pagos_asociados: Optional[List[Dict[str, Any]]] = None,
                                  usuario_ml: Optional[str] = None,
                                  envio_cobro_value: float = 0.0) -> Dict[str, Any]:
    order_id = str(pedido.get("order_id") or "").strip()
    cliente_disp = str(pedido.get("cliente_edit") or "").strip()
    if "—" in cliente_disp:
        cliente_codigo = cliente_disp.split("—", 1)[0].strip()
    else:
        cliente_codigo = cliente_disp or ""

    raw_items = list(pedido.get("line_items") or [])
    items: List[Dict[str, Any]] = []
    for li in raw_items:
        sku_norm = _norm_sku(li.get("sku"))
        new_li = dict(li)
        new_li["sku"] = sku_norm
        items.append(new_li)

    def _line_tot(li) -> float:
        qty = int(li.get("quantity") or 0)
        if li.get("total") is not None or li.get("subtotal") is not None:
            return _to_float(li.get("total") if li.get("total") is not None else li.get("subtotal"))
        unit = _to_float(li.get("price") or 0.0)
        return unit * qty

    total_server = sum(_line_tot(li) for li in items)

    try:
        con = _ensure_db()
    except Exception as ex:
        return {"ok": False, "error": f"No se pudo abrir DB: {ex}", "pedido_id": order_id}
    cur = con.cursor()

    if not (_has_table(cur, "ventas") and _has_table(cur, "it_vent") and _has_table(cur, "paramet") and _has_table(cur, "codigos")):
        try:
            con.close()
        except Exception:
            pass
        return {"ok": False, "error": "Faltan tablas 'ventas'/'it_vent'/'paramet' o 'codigos'.", "pedido_id": order_id}

    try:
        next_mr = _reservar_remito_venta(con)
        tname = _crear_tabla_temporal_remito(con, next_mr)

        vend_up = str(vendor_name or "").strip().upper()
        flex_vendors = {"PATO", "JHONATAN"}
        if es_flex is None:
            es_flex = (vend_up in flex_vendors) or (float(envio_cobro_value or 0.0) > 0.0 and any((_norm_sku(li.get("sku")) == _norm_sku("6696")) for li in items))

        _insert_items_temporales(con, tname, next_mr, items, envio_cost_value=envio_cost_value)
        try:
            rellenar_costos_en_temp(con, tname)
        except Exception:
            pass

        cur.execute(f'SELECT SKU, costo, venta FROM {tname}')
        tmp_rows = cur.fetchall()
        cost_by_sku: Dict[str, float] = {}
        precio_unit_by_sku: Dict[str, float] = {}
        for sku, costo, venta in tmp_rows:
            sku_s = _norm_sku(sku)
            cost_by_sku[sku_s] = float(costo or 0.0)
            precio_unit_by_sku[sku_s] = float(venta or 0.0)

        qty_6696 = sum(int(li.get("quantity") or 0) for li in items if (_norm_sku(li.get("sku")) == _norm_sku("6696")))
        # Blindaje: 6696 (envío FLEX) NO puede ir duplicado; evita facturas con total alterado.
        if qty_6696 > 1:
            raise RuntimeError(f"ERROR: detalle trae 6696 duplicado (qty={qty_6696}). Se cancela la venta para evitar total incorrecto.")

        if qty_6696 > 0:
            if compra_nc_for_envio is not None or (codigo_envio_creado and str(codigo_envio_creado).strip()):
                ok_exact, msg_exact = _reservar_codigo_envio_exacto(
                    con,
                    remito_mr=next_mr,
                    compra_nc=compra_nc_for_envio,
                    codigo_envio=codigo_envio_creado
                )
            else:
                ok_exact, msg_exact = (False, "Sin codigo_envio ni remito de compra para 6696.")
            if not ok_exact:
                taken_fallback = _tomar_codigos_para_remito_compat(con, articulo=_norm_sku("6696"), cantidad=qty_6696, remito=next_mr, deposito=1)
                if len(taken_fallback) != qty_6696:
                    liberar_codigos_de_remito(con, next_mr)
                    raise RuntimeError(f"Stock insuficiente 6696: pedido {qty_6696}, reservado {len(taken_fallback)}. Motivo: {msg_exact}")

        for li in items:
            sku = _norm_sku(li.get("sku"))
            if sku == _norm_sku("6696"):
                continue
            cantidad = int(li.get("quantity") or 0)
            if cantidad <= 0 or not sku:
                continue
            taken = _tomar_codigos_para_remito_compat(con, articulo=sku, cantidad=cantidad, remito=next_mr, deposito=1)
            if len(taken) != cantidad:
                liberar_codigos_de_remito(con, next_mr)
                raise RuntimeError(f"Stock insuficiente {sku}: pedido {cantidad}, reservado {len(taken)}.")

        try:
            cols_cd = _table_cols(cur, "codigos")
            col_rem_ven = "remito_ven" if "remito_ven" in cols_cd else None
            col_dep = _pick(cols_cd, ["deposito"]) if "deposito" in cols_cd else None
            if col_rem_ven and col_dep:
                cur.execute(f"UPDATE codigos SET {col_dep}='' WHERE {col_rem_ven}=?", (int(next_mr),))
                con.commit()
        except Exception:
            pass

        fecha_solo = dt.datetime.now().strftime("%Y-%m-%d")
        cols_v = _table_cols(cur, "ventas")
        col_num = _pick(cols_v, ["venta", "nro", "numero", "id", "mr", "remito"])
        col_cli = _pick(cols_v, ["cliente", "cod_cli", "id_cliente", "cli"])
        col_tot = _pick(cols_v, ["total", "tot", "importe"])
        col_fec = _pick(cols_v, ["fecha", "fech", "fch", "dia"])
        col_gir = _pick(cols_v, ["giros", "giro", "neto", "neto_giros", "total_giros"])
        tipo_envio_up = str(pedido.get("tipo_envio") or "").strip().upper()
        is_me_tab_flag = bool(pedido.get("is_me_tab") or False)
        es_mercado_envios = (
            is_me_tab_flag
            or (tipo_envio_up in {"MERCADO ENVIOS", "MERCADOENVIO", "MERCADO_ENVIO", "ME2"})
            or ("MERCADO" in tipo_envio_up and "ENV" in tipo_envio_up)
        )
        cliente_final = "889" if es_flex else ("100" if es_mercado_envios else (cliente_codigo if cliente_codigo else "0"))
        cur.execute(
            f'INSERT INTO ventas ("{col_num}","{col_cli}","{col_tot}","{col_fec}","{col_gir}") VALUES (?,?,?,?,?)',
            (int(next_mr), str(cliente_final or ""), float(total_server or 0.0), fecha_solo, float(total_server or 0.0))
        )

        cols_i = _table_cols(cur, "it_vent")
        remito_col = _pick(cols_i, ["remito", "mr", "nroremito", "id_remito"])
        venta_col = "venta" if any(c.lower() == "venta" for c in cols_i) else None
        art_i = _pick(cols_i, ["articulo", "codigo", "sku", "cod_art"])
        cant_i = _pick(cols_i, ["cant", "cantidad"])
        precio_i = _pick(cols_i, ["precio", "precio_unit", "punit", "importe"])
        costo_col = None
        for cand in ["costo", "cost", "precio_costo", "pcosto"]:
            if cand in cols_i or cand.lower() in [c.lower() for c in cols_i]:
                costo_col = _pick(cols_i, [cand])
                break

        for li in items:
            sku = _norm_sku(li.get("sku"))
            cant = int(li.get("quantity") or 0)
            price_unit = precio_unit_by_sku.get(sku, 0.0)
            if price_unit is None:
                price_unit = 0.0

            if (not price_unit) or price_unit <= 0:
                line_total = _line_tot(li)
                price_unit = (line_total / cant) if (cant and line_total) else _to_float(li.get("price") or 0.0)

            # COSTO: prioridad a costo explícito del item (para 6711 costo-only)
            explicit_cost = _to_float(li.get("costo") if li.get("costo") is not None else 0.0)
            if explicit_cost > 0:
                cost_unit = float(explicit_cost)
            else:
                cost_unit = cost_by_sku.get(sku, None)
                if cost_unit is None:
                    cost_unit = costo_para_it_vent(con, sku, li.get("costo"))

            if venta_col and costo_col:
                cur.execute(
                    f'INSERT INTO it_vent ("{remito_col}","{art_i}","{cant_i}","{precio_i}","{costo_col}","{venta_col}") VALUES (?,?,?,?,?,?)',
                    (int(next_mr), sku, int(cant), float(price_unit), float(cost_unit), float(price_unit))
                )
            elif venta_col and not costo_col:
                cur.execute(
                    f'INSERT INTO it_vent ("{remito_col}","{art_i}","{cant_i}","{precio_i}","{venta_col}") VALUES (?,?,?,?,?)',
                    (int(next_mr), sku, int(cant), float(price_unit), float(price_unit))
                )
            elif not venta_col and costo_col:
                cur.execute(
                    f'INSERT INTO it_vent ("{remito_col}","{art_i}","{cant_i}","{precio_i}","{costo_col}") VALUES (?,?,?,?,?)',
                    (int(next_mr), sku, int(cant), float(price_unit), float(cost_unit))
                )
            else:
                cur.execute(
                    f'INSERT INTO it_vent ("{remito_col}","{art_i}","{cant_i}","{precio_i}") VALUES (?,?,?,?)',
                    (int(next_mr), sku, int(cant), float(price_unit))
                )

        con.commit()

        pagos_info = list(pagos_asociados or [])
        usuario_ml_s = str(usuario_ml).strip() if (usuario_ml is not None and str(usuario_ml).strip() != "") else "CANDY-HO"
        now_s = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        _ensure_movimientos_mp(cur)
        _ensure_envios_flex(cur)

        mov_cols = _table_cols(cur, "movimientos_mp")
        has_pack_col = any(c.upper() == "PACK" for c in mov_cols)
        has_tipo_col = any(c.upper() == "TIPO" for c in mov_cols)

        # Si es MERCADO ENVIOS, marcar tipo para insertar en la columna TIPO (si existe)
        tipo_val_for_insert = "MERCADOENVIO" if es_mercado_envios else None

        if pagos_info:
            # Construir filas teniendo en cuenta PACK y TIPO si existen
            rows_to_insert = []
            if has_pack_col and has_tipo_col:
                for p in pagos_info:
                    rows_to_insert.append((
                        str(p.get("orden") or p.get("order") or ""),
                        str(p.get("pago_id") or p.get("id") or ""),
                        now_s,
                        float(p.get("neto") or 0.0),
                        int(next_mr),
                        usuario_ml_s,
                        str(p.get("pack") or "") if p.get("pack") is not None else None,
                        tipo_val_for_insert
                    ))
                cur.executemany(
                    "INSERT INTO movimientos_mp (Orden, movimiento, fecha, importe, remito_venta, usuario_ml, PACK, TIPO) VALUES (?,?,?,?,?,?,?,?)",
                    rows_to_insert
                )
            elif has_pack_col and not has_tipo_col:
                for p in pagos_info:
                    rows_to_insert.append((
                        str(p.get("orden") or p.get("order") or ""),
                        str(p.get("pago_id") or p.get("id") or ""),
                        now_s,
                        float(p.get("neto") or 0.0),
                        int(next_mr),
                        usuario_ml_s,
                        str(p.get("pack") or "") if p.get("pack") is not None else None,
                    ))
                cur.executemany(
                    "INSERT INTO movimientos_mp (Orden, movimiento, fecha, importe, remito_venta, usuario_ml, PACK) VALUES (?,?,?,?,?,?,?)",
                    rows_to_insert
                )
            elif (not has_pack_col) and has_tipo_col:
                for p in pagos_info:
                    rows_to_insert.append((
                        str(p.get("orden") or p.get("order") or ""),
                        str(p.get("pago_id") or p.get("id") or ""),
                        now_s,
                        float(p.get("neto") or 0.0),
                        int(next_mr),
                        usuario_ml_s,
                        tipo_val_for_insert
                    ))
                cur.executemany(
                    "INSERT INTO movimientos_mp (Orden, movimiento, fecha, importe, remito_venta, usuario_ml, TIPO) VALUES (?,?,?,?,?,?,?)",
                    rows_to_insert
                )
            else:
                for p in pagos_info:
                    rows_to_insert.append((
                        str(p.get("orden") or p.get("order") or ""),
                        str(p.get("pago_id") or p.get("id") or ""),
                        now_s,
                        float(p.get("neto") or 0.0),
                        int(next_mr),
                        usuario_ml_s,
                    ))
                cur.executemany(
                    "INSERT INTO movimientos_mp (Orden, movimiento, fecha, importe, remito_venta, usuario_ml) VALUES (?,?,?,?,?,?)",
                    rows_to_insert
                )

        cobro = float(envio_cobro_value or 0.0) if es_flex else 0.0
        costo = float(envio_cost_value or 0.0)
        gan = cobro - costo
        cur.execute(
            "INSERT INTO envios_flex (remito, fecha, motoquero, costo_de_envio, cobro_de_envio, ganancia) VALUES (?,?,?,?,?,?)",
            (int(next_mr), now_s, str(vend_up or ""), costo, cobro, gan)
        )

        con.commit()

    except Exception as ex_ins:
        try:
            liberar_codigos_de_remito(con, next_mr)
        except Exception:
            pass
        try:
            con.rollback()
        except Exception:
            pass
        try:
            con.close()
        except Exception:
            pass
        return {"ok": False, "error": f"Alta de venta falló: {ex_ins}", "pedido_id": order_id}

    try:
        con.close()
    except Exception:
        pass
    return {"ok": True, "remito": str(next_mr), "pedido_id": order_id}
