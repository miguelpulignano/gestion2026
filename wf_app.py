# -*- coding: utf-8 -*-
import os
# === Logging (GESTION2026) ===
import logging, os
def _ensure_logs_dir():
    try: os.makedirs(r"C:\!GESTION2026\LOGS", exist_ok=True)
    except Exception: pass
_ensure_logs_dir()
WF_APP_LOG = logging.getLogger("GESTION2026.WEB_FACTURATOR")
if not any(getattr(h, "_g26_tag", "") == "web_facturator.log" for h in WF_APP_LOG.handlers):
    _fh = logging.FileHandler(os.path.join(r"C:\!GESTION2026\LOGS", "web_facturator.log"), encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    _fh._g26_tag = "web_facturator.log"
    WF_APP_LOG.addHandler(_fh)
WF_APP_LOG.setLevel(logging.INFO)

import datetime as dt
import sqlite3
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter import simpledialog as sd

# Ventana de consulta: rango de días configurable
DAYS_WINDOW = 15

from wf_constants import *
from wf_utils import _maximize_or_fit, get_logger, money, leer_json, guardar_json
from wf_db import ensure_stock_connection, fetch_clientes_all, lookup_nombre_por_codigo
from wf_clients import ClientPicker
from wf_woo import fetch_orders_demo, fetch_orders_woo, hay_cfg_woo
from wf_preview import PreviewWindow

# ACTIVAR HOOK COMPLETO (precompra/validaciones/mensajería)
import wf_preview_hook  # carga y parchea PreviewWindow

# dependencias para alta silenciosa
from gestion_config import log
from gestion_db import (
    get_mr_and_prepare_tr,
    codigos_para_articulo,
    itc_sugerido_y_costo,
    tr_insert_lotes,
    drop_tr_table,
)
from ventas_ops import confirmar_venta

# NUEVO: compra silenciosa de envío (archivo externo, opcional)
try:
    from wf_envios_compra import compra_silenciosa_envio
except Exception:
    compra_silenciosa_envio = None

# NUEVO: expansión de SKUs con '+' (módulo solicitado: wf_split_plus.py)
try:
    from wf_split_plus import expand_plus_skus
except Exception:
    expand_plus_skus = None

logger = get_logger()

# --------- Filtro por últimos N días ---------
def _parse_date_any(s: str) -> dt.date | None:
    if not s: return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except Exception:
            continue
    try:
        return dt.datetime.fromisoformat(s.replace("Z",""))
    except Exception:
        return None


def _filter_last_days(rows, days=DAYS_WINDOW):
    """Devuelve solo pedidos cuya fecha >= hoy - days."""
    try:
        today = dt.date.today()
        cutoff = today - dt.timedelta(days=int(days))
    except Exception:
        cutoff = dt.date.today() - dt.timedelta(days=DAYS_WINDOW)
    out = []
    for r in (rows or []):
        d = _parse_date_any(r.get("date") or r.get("order_date") or "")
        if d and d >= cutoff:
            out.append(r)
        elif not d:
            out.append(r)
    return out


def _normalize_order_for_top(o: dict) -> dict:
    raw = o.get("_raw") or {}
    bill = raw.get("billing") or {}
    fname = (bill.get("first_name") or "").strip()
    lname = (bill.get("last_name") or "").strip()
    customer = (f"{fname} {lname}").strip()

    status = (raw.get("status") or "").strip()

    items = 0
    for li in (o.get("line_items") or []):
        try:
            items += int(li.get("quantity") or 0)
        except Exception:
            pass

    cargo_envio = None
    try:
        sl = raw.get("shipping_lines") or []
        if sl:
            cargo_envio = float(sl[0].get("total") or 0)
    except Exception:
        cargo_envio = None

    try:
        _, cliente_disp = self._auto_cliente(o.get("shipping") or "", o.get("payment") or "")
    except Exception:
        cliente_disp = "CLIENTE: No se sabe"

    try:
        total_val = float(o.get("total") or 0)
    except Exception:
        total_val = 0.0

    # Lógica adicional para "Retiro por el local: Gratis" cuando el pago es "Tarjeta en el local"
    if (o.get("payment") or "").strip().lower() == "tarjeta en el local":
        o["shipping"] = "Retiro por el local: Gratis"

    fact = "SI" if self._pedido_facturado_lookup(str(o.get("order_id") or "")) else "NO"

    return {
        "order_id": o.get("order_id", ""),
        "date": o.get("date", ""),
        "facturado": fact,
        "customer": customer,
        "status": status,
        "items": items,
        "shipping": o.get("shipping", ""),
        "cargo_envio": cargo_envio,
        "cliente": cliente_disp,
        "payment": o.get("payment", ""),
        "total": total_val,
    }


class WebFacturator(tk.Tk):
    def _load_from_cache_or_fetch(self):
        try:
            from wf_constants import CACHE_PATH
            from wf_utils import leer_json, guardar_json
            from wf_woo import fetch_orders_demo, fetch_orders_woo, hay_cfg_woo
        except Exception as e:
            messagebox.showerror("Import error", f"No se pudieron importar dependencias: {e}")
            return

        self.order_by_id.clear()

        cache = None
        try:
            cache = leer_json(CACHE_PATH)
        except Exception:
            cache = None

        if not cache or not isinstance(cache, dict) or not cache.get("rows"):
            self._cancel_flag = False
            self._show_overlay("Actualizando...", cancellable=True)
            try:
                if hay_cfg_woo():
                    rows_all = fetch_orders_woo({})
                else:
                    rows_all = fetch_orders_demo({})
                rows = _filter_last_days(rows_all, days=DAYS_WINDOW)
            finally:
                self._hide_overlay()
            if self._cancel_flag:
                return

            for r in rows:
                ship_txt = r.get("shipping") or ""
                pago_txt = (r.get("payment") or "").strip().lower()
                if pago_txt == "tarjeta en el local":
                    ship_txt = "Retiro por el local: Gratis"  # Aplicar lógica solicitada
                code_cli, display_cli = self._auto_cliente(ship_txt, pago_txt)
                r["cliente_code"] = code_cli or ""
                r["cliente"] = display_cli
                r["facturado"] = "SI" if self._pedido_facturado_lookup(str(r.get("order_id") or "")) else "NO"
                oid = str(r.get("order_id") or "")
                if oid:
                    self.order_by_id[oid] = dict(r)

            self._populate_orders(rows)
            meta = {"source": "API", "count": len(rows)}
            try:
                guardar_json(CACHE_PATH, {"meta": meta, "rows": rows})
            except Exception:
                pass
            self._set_fuente(meta)
            self._refresh_status(f"Consultado API (últimos {DAYS_WINDOW} días). {len(rows)} pedidos.")
        else:
            rows = (cache.get("rows") or [])
            rows = _filter_last_days(rows, days=DAYS_WINDOW)
            for r in rows:
                oid = str(r.get("order_id") or "")
                pago_txt = (r.get("payment") or "").strip().lower()
                if pago_txt == "tarjeta en el local":
                    r["shipping"] = "Retiro por el local: Gratis"  # Aplicar lógica solicitada
                if oid:
                    r["facturado"] = "SI" if self._pedido_facturado_lookup(oid) else "NO"
                    self.order_by_id[oid] = dict(r)
            self._populate_orders(rows)
            meta = cache.get("meta") or {"source": "CACHE"}
            self._set_fuente(meta)
            self._refresh_status(f"Cargado desde CACHE (últimos {DAYS_WINDOW} días). {len(rows)} pedidos.")

if __name__ == "__main__":
    app = WebFacturator()
    app.mainloop()