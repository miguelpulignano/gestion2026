# -*- coding: utf-8 -*-
# GESTION2026 - Confirmación de venta optimizada
# Reglas inviolables:
#  - Usar nombres de columnas EXACTOS; si falta alguna => abortar con error claro.
#  - Loguear columnas detectadas por tabla (si el log está habilitado por el caller).
#  - Transacción atómica; rollback ante cualquier error.
#  - Fecha en DB: YYYY-MM-DD.
#  - Cliente normalizado a 3 dígitos si es numérico.
#  - Sin tocar SERIE ni otras tablas no solicitadas.
#
# Ajustes:
#  - Bloqueo: NO permitir impactar it_vent con costo <= 0 (error y rollback).
#  - Búsqueda de costo con SKU NORMALIZADO (zfill a 4 si es numérico corto).
#  - Guardar it_vent.articulo NORMALIZADO a 4 cifras (cero a la izquierda).

import sqlite3
from datetime import datetime

DB_PATH = r"C:\!GESTION2026\gestion.sqlite3"

def _columns(conn, table: str):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]

def _require_columns(conn, table: str, required: list, log):
    cols = _columns(conn, table)
    try:
        log(f'[{table}] Columnas detectadas: ' + ', '.join(cols))
    except Exception:
        pass
    missing = [c for c in required if c not in cols]
    if missing:
        raise RuntimeError(f"FALTAN columnas en {table}: {', '.join(missing)}")
    return cols

def _fmt_today_sql() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def _pad_cliente_codigo(codigo) -> str:
    s = str(codigo).strip()
    return s.zfill(3) if s.isdigit() else s

def _norm_sku(sku: str) -> str:
    """
    Normaliza el SKU si es puramente numérico y de longitud < 4, zfill(4).
    Caso contrario lo deja igual.
    """
    s = str(sku).strip()
    if s.isdigit() and len(s) < 4:
        return s.zfill(4)
    return s

def confirmar_venta(MR, cliente_codigo, total, efectivo, transferencia,
                    tree_rows, tr_table, log, progress=None):
    """Impacta la venta en DB con operaciones en bloque y validaciones estrictas.
    Progress (opcional) se invoca con units enteros tras cada paso relevante:
      - clientes: 1
      - ventas: 1
      - it_vent: len(tree_rows)
      - TR->codigos: tr_count (como salto final)
    """
    if progress is None:
        progress = lambda n=1: None

    cliente_codigo = _pad_cliente_codigo(cliente_codigo)
    if not cliente_codigo:
        raise RuntimeError("Cliente no seleccionado.")

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN")

        # --- Validaciones de columnas ---
        _require_columns(conn, "clientes", ["saldo", "codigo"], log)
        _require_columns(conn, "ventas",   ["remito","fecha","cliente","total","efectivo","cheques","giros"], log)
        if tree_rows:
            _require_columns(conn, "it_vent", ["remito","articulo","cantidad","costo","venta"], log)
            _require_columns(conn, tr_table, ["SKU","CODIGO","remito","costo","Precio de Venta"], log)
        _require_columns(conn, "codigos",  ["codigo","remito_ven","deposito"], log)

        # --- Tamaño TR e índices opcionales (solo si conviene) ---
        tr_count = conn.execute(f"SELECT COUNT(*) FROM {tr_table}").fetchone()[0] or 0
        if tr_count > 200:
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{tr_table}_cod ON {tr_table}(CODIGO)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{tr_table}_sku ON {tr_table}(SKU)")

        # --- 1) CLIENTES: actualizar saldo ---
        cur = conn.execute(
            "UPDATE clientes SET saldo = saldo - ? - ? + ? WHERE codigo = ?",
            (float(efectivo or 0), float(transferencia or 0), float(total or 0), cliente_codigo),
        )
        if cur.rowcount == 0:
            raise RuntimeError(f"Cliente '{cliente_codigo}' no encontrado en clientes.")
        progress(1)

        # --- 2) VENTAS: insertar cabecera ---
        fecha_txt = _fmt_today_sql()
        conn.execute(
            "INSERT INTO ventas(remito, fecha, cliente, total, efectivo, cheques, giros) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(MR), fecha_txt, cliente_codigo, float(total or 0),
             float(efectivo or 0), 0.0, float(transferencia or 0)),
        )
        progress(1)

        # --- 3) IT_VENT: inserción en bloque con búsqueda NORMALIZADA y bloqueo costo=0 ---
        if tree_rows:
            # Promedio de costo por SKU en TR
            avg_cost_rows = conn.execute(
                f"SELECT SKU, AVG(costo) AS avg_costo FROM {tr_table} GROUP BY SKU"
            ).fetchall()

            # Mapear ambos: SKU tal cual y SKU normalizado (si aplica)
            avg_cost = {}
            for sku_raw, avg in avg_cost_rows:
                sku_str = str(sku_raw)
                costo_val = 0.0 if avg is None else float(avg)
                avg_cost[sku_str] = costo_val
                norm = _norm_sku(sku_str)
                if norm != sku_str and norm not in avg_cost:
                    avg_cost[norm] = costo_val  # permitir lookup por normalizado

            it_rows = []
            for row in tree_rows:
                sku_original = str(row["sku"]).strip()
                sku_norm = _norm_sku(sku_original)
                cantidad = int(row["cantidad"])
                venta = float(row["precio_unit"])

                # Buscar costo primero por SKU original, luego por normalizado
                costo_prom = float(avg_cost.get(sku_original, avg_cost.get(sku_norm, 0.0)))

                # Bloqueo costo<=0
                if costo_prom <= 0.0:
                    raise RuntimeError(
                        f"Costo 0 para artículo '{sku_original}' (normalizado '{sku_norm}'). "
                        f"No se permite impactar it_vent con costo=0. Revisar compras / TR antes de confirmar."
                    )

                # Guardar it_vent.articulo NORMALIZADO (4 cifras con ceros a la izquierda)
                articulo_para_guardar = sku_norm

                it_rows.append((str(MR), articulo_para_guardar, cantidad, costo_prom, venta))

            # Inserción
            conn.executemany(
                "INSERT INTO it_vent(remito, articulo, cantidad, costo, venta) VALUES(?,?,?,?,?)",
                it_rows
            )
            progress(len(it_rows))

            # Verificación de sanidad post-inserción
            bad = conn.execute(
                "SELECT articulo, costo FROM it_vent WHERE remito = ? AND (costo IS NULL OR costo <= 0) LIMIT 1",
                (str(MR),)
            ).fetchone()
            if bad:
                raise RuntimeError(
                    f"Se detectó costo 0 en it_vent tras insertar (artículo '{bad[0]}', MR {MR}). "
                    f"Se anula la venta."
                )

        # --- 4) TR -> CODIGOS: validación + UPDATE masivo ---
        miss = conn.execute(
            f"SELECT t.CODIGO FROM {tr_table} t "
            "LEFT JOIN codigos c ON c.codigo = t.CODIGO "
            "WHERE c.codigo IS NULL LIMIT 1"
        ).fetchone()
        if miss:
            raise RuntimeError(f"Anomalía: código '{miss[0]}' no existe en CODIGOS.")

        conn.execute(
            f"UPDATE codigos SET remito_ven = ?, deposito = ' ' "
            f"WHERE codigo IN (SELECT CODIGO FROM {tr_table})",
            (str(MR),)
        )
        progress(int(tr_count or 0))

        conn.execute("COMMIT")
        try:
            log(f"Venta confirmada MR={MR}; filas TR procesadas={tr_count}; items={len(tree_rows or [])}")
        except Exception:
            pass
        return True

    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()