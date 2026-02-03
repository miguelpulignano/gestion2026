# -*- coding: utf-8 -*-
"""
Pantalla de FACTURAR — Archivo completo y listo para reemplazar

Correcciones y mejoras claves pedidas:
- Respeta SIEMPRE el vendedor elegido en la UI (owner.var_vendor) y normaliza a "CANDYHO"/"OMYTECH".
- Aumentado +5 líneas la tabla de Pagos ID (height=12).
- Regla FLEX: pagos > 33.000 => neto = monto (y mostrar "Neto recibido modificado").
- Ocultar encabezado "ERROR" si err_count == 0.
- Validaciones compra silenciosa 6696: NC > 100000 -> ERROR; proveedor==0 -> ERROR y cancelar venta.
- Expansión de KITS ("kit...") con componentes desde DB, nombre por articulo.descrip.
- Multiplicadores:
  * SKU "NNNNxM": cantidad *= M y precio /= M.
  * Nombre: xN en los primeros 4 caracteres. Caso especial: si el nombre contiene la palabra completa
    "resistencia" o "resistencias" (límite de palabra, case-insensitive), dividir el multiplicador por 10.
    No impacta palabras compuestas como "fotorresistencias".
  * Descripción “PACK xN”/“PACK XN”: cantidad *= N y precio /= N.
  * NUEVO: Si la descripción del artículo contiene la palabra "metros" (case-insensitive),
    y hasta 4 caracteres antes hay un número, multiplicar la cantidad por ese número y dividir el precio por ese número.
    Ejemplos: "10 metros", "10metros", "  3   Metros", "25METROS" -> M = 10, 10, 3, 25.
  * EXCEPCIÓN: Si el SKU es "5283", NO aplicar la regla "metros" en descripción.
  * EXCEPCIÓN ACTUALIZADA:
      - Para SKU 5293: aplicar ÚNICAMENTE la regla de la 'x' en el SKU (NNNNxM). NO aplicar xN en nombre, PACK xN en descripción ni 'metros'.
      - Para SKU 6408: NO aplicar ninguna regla de multiplicación (se vende tal cual).

CAMBIO SOLICITADO (enero 2026):
- Chequeo de totales (tol 0.50):
  * Normal: comparar TOTAL de la factura contra la SUMA de "Neto recibido".
  * Excepción: si algún Pago ID tiene "Cuotas" (installments) > 1, entonces comparar TOTAL de la factura
    contra la SUMA de "Monto" (porque el neto puede diferir por comisiones/financiación).
- Si el total de la factura NO coincide con el valor de referencia (Neto/Monto según la regla),
  entonces, si existe algún "Env. Pago" en los pagos aprobados, ese monto se suma al precio del ítem 6696
  (o se agrega 6696 si no existiera) y se vuelve a chequear (solo cuando el chequeo es por NETO, no en cuotas>1).
  Si aún así no coincide, antes de marcar ERROR se prueba el caso excepcional:
    * Si (SUMA MONTO recibido + SUMA ENVIO) coincide con el TOTAL de la factura (tol 0.50),
      NO es error: avisar "CASO EXEPCIONAL MONTO + ENVIO" y dejar facturar.
  Si tampoco coincide, se marca ERROR y NO se permite facturar esa pestaña.

NUEVO previamente:
- Eliminar pagos rechazados: si estado == "rejected", no se muestran ni se totalizan (ni monto ni neto).
- Removida la verificación y mensaje "Suma it_vent.venta vs Neto Pagos".
- Para facturas de RETIRO DEL LOCAL (tipo_envio = "RETIRA" o "ACUERDO CON EL VENDEDOR"), NO exigir costo de envío seteado.
- Permitir facturar también "MERCADO ENVIOS" (además de FLEX/RETIRA).
  (La habilitación de selección/preview para MERCADO ENVIOS se maneja en la Vista Previa/tabla principal,
   este archivo solo factura lo que ya venga seleccionado.)

IMPORTANTE (corrección solicitada por el usuario):
- Para MERCADO ENVIOS, el costo de envío vendedor (env_vend) NO debe tomar shipment.base_cost si en shipment.shipping_option.list_cost
  existe un valor > 0. Debe preferirse list_cost. Esto corrige casos donde base_cost aparece duplicado o inconsistente.

NUEVO (febrero 2026 - SKU 6756 ENVÍO COMPARTIDO):
- Para MERCADO ENVIOS con "envío compartido" (parte a cargo nuestro y parte bonificada por ML):
  * SKU 6711: Representa "nuestro costo de envío" (lo que pagamos al proveedor 034).
  * SKU 6756: Representa la "bonificación de MercadoLibre" (diferencia entre shipping_cost_seller y nuestro_costo_envio).
  * Cuando shipping_cost_seller > nuestro_costo_envio, se crea automáticamente:
    - Compra silenciosa de SKU 6756 (proveedor 034, costo=0)
    - Item en factura: "Envio Bonificacion de MercadoLibre", precio = bonificacion_ml, costo = 0
  * Esto permite facturar correctamente casos donde ML subsidia parte del envío.




DEG DICE:
PARA EXCEPCIONES AL MULTIPLICADOR POR x EN LA DESCRIPCION BUSCAR 1637 y COPIAR PROCEDIMIENTO

PARA EXCEPCIONES A LA REGLA PACK 6404
"""

from ml_facturador_ui_data_common import (
    Any, Dict, List, Optional, Tuple,
    datetime, timezone, timedelta,
    json, os, sqlite3, tempfile,
    tk, filedialog, messagebox, ttk,
    AR_TZ, TMP_CONSULT_PATH, FALLBACK_JSON_PATH,
)
import re
import unicodedata
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate
import ml_facturator_ventas_ops as ventas_ops

# Literales EXACTOS esperados
ML_VENDEDORES = ("CANDYHO", "OMYTECH")
ml_vendedor: str = "CANDYHO"  # Fallback global (si la UI no expone var_vendor)


# --------------------- Helpers generales ---------------------

def _norm_sku(sku: Any) -> str:
    raw = str(sku or "").strip()
    if not raw:
        return ""
    return raw.zfill(4) if raw.isdigit() else raw


# ---------------------------------------------------------------------------
# SKU SIN REGLAS
# Agregar acá SKUs que NO deben recibir ciertas reglas automáticas.
# Formato: {"SKU": {"PACK", "MULT", ...}}
# "PACK" => deshabilita multiplicadores por nombre/descripcion PACK para ese SKU.
# "MULT" => deshabilita TODAS las reglas de multiplicación de cantidad (SKU NNNNxM, PACK, nombre xN, 'metros').
SKUS_SIN_REGLAS = {
    "6628": {"PACK"},
    "1346": {"PACK"},
    "6408": {"MULT"},  # 6408 sin ninguna regla de multiplicación
    # 5293 se maneja con una excepción específica (solo aplica NNNNxM en el SKU)
}

def _sku_skip_rule(sku_base_norm: Any, rule: str) -> bool:
    """Devuelve True si ese SKU debe saltear una regla automática."""
    try:
        sku_key = str(sku_base_norm or "").strip()
        return rule in SKUS_SIN_REGLAS.get(sku_key, set())
    except Exception:
        return False

def _strip_accents(s: str) -> str:
    """Quita acentos para comparaciones tolerantes (RETIRÓ == RETIRO)."""
    try:
        nfkd = unicodedata.normalize('NFKD', s)
        return ''.join(ch for ch in nfkd if not unicodedata.combining(ch))
    except Exception:
        return s


def _norm_tipo_envio(tipo: Any) -> str:
    t = str(tipo or '').strip().upper()
    t = ' '.join(t.split())
    t = _strip_accents(t)
    return t


def _is_pickup_tipo_envio(tipo: Any) -> bool:
    """True si el tipo de envío representa retiro en el local / acuerdo (sin costo de envío)."""
    t = _norm_tipo_envio(tipo)
    if not t:
        return False

    pickup_exact = {
        "RETIRA",
        "RETIRA DEL LOCAL",
        "RETIRA POR EL LOCAL",
        "RETIRO POR EL LOCAL",
        "RETIRO EN EL LOCAL",
        "RETIRAR EN EL LOCAL",
        "ACUERDO CON EL VENDEDOR",
    }
    if t in pickup_exact:
        return True

    pickup_prefixes = (
        "RETIRA DEL LOCAL",
        "RETIRA POR EL LOCAL",
        "RETIRO POR EL LOCAL",
        "RETIRO EN EL LOCAL",
        "RETIRAR EN EL LOCAL",
    )
    return any(t.startswith(p) for p in pickup_prefixes)


def _normalize_vendor_literal(v: str) -> str:
    up = (str(v or "").strip().upper()
          .replace(" ", "")
          .replace("-", "")
          .replace("_", ""))
    if up.startswith("CANDY") and "OMY" not in up:
        return "CANDYHO"
    if "OMY" in up:
        return "OMYTECH"
    if up in ML_VENDEDORES:
        return up
    return "CANDYHO"


def _set_ml_vendedor(value: str):
    """Actualiza fallback global (compatibilidad externa)."""
    global ml_vendedor
    ml_vendedor = _normalize_vendor_literal(value)


def _first_stringvar_value(owner) -> Optional[str]:
    """Devuelve el valor del primer StringVar encontrado en el owner (si existe)."""
    try:
        for name in dir(owner):
            try:
                attr = getattr(owner, name)
            except Exception:
                continue
            if hasattr(attr, "get") and "StringVar" in attr.__class__.__name__:
                try:
                    return attr.get()
                except Exception:
                    pass
        return None
    except Exception:
        return None


def _extract_tipo_envio(o: Any) -> str:
    """Devuelve el tipo de envío desde un dict de orden, tolerando distintas claves/estructuras."""
    if not isinstance(o, dict):
        return ""
    v = (
        o.get("tipo_envio")
        or o.get("tipo")
        or o.get("shipping_type")
        or o.get("shipping_mode")
        or o.get("shipping_method")
        or o.get("shipping_method_title")
        or o.get("shipping")
        or o.get("envio")
        or ""
    )
    if isinstance(v, dict):
        v = v.get("tipo") or v.get("type") or v.get("mode") or v.get("name") or v.get("title") or ""
    try:
        return str(v or "").strip()
    except Exception:
        return ""


def _resolve_usuario_ml(owner) -> str:
    """
    RESPECTA SIEMPRE el vendedor elegido en la UI:
    - Si existe owner.var_vendor, toma su .get() y normaliza.
    - Si no existe, se intenta primer StringVar del owner.
    - Fallback final: ml_vendedor global.
    """
    try:
        if hasattr(owner, "var_vendor") and getattr(owner, "var_vendor") is not None:
            val = owner.var_vendor.get()
            if val:
                return _normalize_vendor_literal(val)
    except Exception:
        pass
    try:
        sv_val = _first_stringvar_value(owner)
        if sv_val:
            return _normalize_vendor_literal(sv_val)
    except Exception:
        pass
    return _normalize_vendor_literal(ml_vendedor)


def _robust_parse_number(s: Any) -> float:
    """Parser robusto (US/AR) para números (precios totales/subtotales)."""
    if isinstance(s, (int, float)):
        try:
            return float(s)
        except Exception:
            return 0.0
    try:
        txt = "" if s is None else str(s).strip()
    except Exception:
        txt = ""
    if txt == "":
        return 0.0
    t = txt.replace("$", "").strip()
    dot_count = t.count(".")
    comma_count = t.count(",")
    try:
        if dot_count and comma_count:
            if t.rfind(".") > t.rfind(","):
                return float(t.replace(",", ""))
            else:
                return float(t.replace(".", "").replace(",", "."))
        elif comma_count and not dot_count:
            if comma_count > 1:
                return float(t.replace(",", ""))
            else:
                return float(t.replace(",", "."))
        elif dot_count and not comma_count:
            if dot_count > 1:
                return float(t.replace(".", ""))
            else:
                return float(t)
        else:
            return float(t)
    except Exception:
        try:
            return float(str(s).replace(".", "").replace(",", "."))
        except Exception:
            return 0.0


def _safe_float(x: Any) -> float:
    """Parsea números soportando AR (1.234,56) y US (1,234.56 / 1234.56).
    Evita el bug de remover el punto decimal cuando viene con '.' decimal.
    """
    return _robust_parse_number(x)


def _infer_envio_seller_for_order(o: Dict[str, Any]) -> float:
    """
    Corrige el cálculo del envío vendedor, con prioridad:
    - Si tipo_envio == "MERCADO ENVIOS": usar shipment_raw.shipping_option.list_cost (si > 0),
      si no, usar shipping_option.list_cost (top-level) (si > 0),
      si no, fallback a shipping_cost_seller existente.
    - Para otros tipos: respetar shipping_cost_seller existente.

    Esto evita duplicar base_cost cuando shipment.base_cost refleja otro monto y list_cost es el correcto.
    """
    try:
        tipo = (o.get("tipo_envio") or "").strip().upper()
    except Exception:
        tipo = ""

    current = _safe_float(o.get("shipping_cost_seller") or 0.0)

    if tipo != "MERCADO ENVIOS":
        return current

    try:
        sh_raw = o.get("shipment_raw") or {}
    except Exception:
        sh_raw = {}

    # 1) shipment_raw.shipping_option.list_cost
    try:
        so = (sh_raw.get("shipping_option") or {}) if isinstance(sh_raw, dict) else {}
        lc = _safe_float(so.get("list_cost"))
        if lc > 0:
            return lc
    except Exception:
        pass

    # 2) top-level parsed shipping_option.list_cost (si existe)
    try:
        so2 = (o.get("shipping_option") or {}) if isinstance(o.get("shipping_option"), dict) else {}
        lc2 = _safe_float(so2.get("list_cost"))
        if lc2 > 0:
            return lc2
    except Exception:
        pass

    return current

def _is_skippable_payment_status(status_raw: Any) -> bool:
    """
    Retorna True si el estado del pago debe ser ignorado en UI/validaciones.
    Cubre: 'rejected', 'cancelled', 'canceled' y variantes que comiencen con 'reject' o 'cancel'.
    """
    try:
        s = str(status_raw or "").strip().lower()
    except Exception:
        s = ""
    if not s:
        return False
    if s in {"rejected", "cancelled", "canceled"}:
        return True
    if s.startswith("reject") or s.startswith("cancel"):
        return True
    return False


def _group_has_envio_item(orders_group: List[Dict[str, Any]]) -> bool:
    """
    Devuelve True si en el detalle del grupo aparece un item que represente ENVIO.
    Se usa para decidir si corresponde exigir/registrar costo de envío en MERCADO ENVIOS.

    Heurística:
    - Si el nombre/título contiene "ENVIO" (sin importar tildes/mayúsculas).
    - O si el SKU coincide con alguno de los SKUs típicos de envío (6696/6711/0888).
    """
    try:
        import unicodedata

        def _norm(s: Any) -> str:
            s = str(s or "").strip().upper()
            s = "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")
            return s
    except Exception:

        def _norm(s: Any) -> str:
            return str(s or "").strip().upper()

    env_skus = {_norm_sku("6696"), _norm_sku("6711"), _norm_sku("6756"), _norm_sku("0888")}

    for o in (orders_group or []):
        items = o.get("productos") or o.get("line_items") or []
        for li in (items or []):
            name = li.get("title") or li.get("name") or li.get("descripcion") or ""
            sku = li.get("sku") or li.get("articulo") or li.get("codigo") or ""
            if "ENVIO" in _norm(name):
                return True
            try:
                if _norm_sku(sku) in env_skus:
                    return True
            except Exception:
                pass
    return False


# --------------------- Multiplicadores ---------------------

def _pack_multiplier_from_name(name: Any) -> float:
    """
    Detecta 'n x'/'n X' en los primeros caracteres del nombre (ej. '3x', '2X', '10 X').
    - Case-insensitive y tolera espacios entre número y 'x'.
    - Si el nombre contiene la palabra completa 'resistencia' o 'resistencias' (con límites de palabra),
      divide el multiplicador por 10. No afecta palabras compuestas como 'fotorresistencias'.
    """
    try:
        s = str(name or "")
    except Exception:
        return 1.0
    head = s[:5]
    m = re.search(r'^\s*(\d+)\s*[xX]', head)
    mult = 1.0
    if m:
        try:
            n = int(m.group(1))
            mult = float(n if n > 0 else 1)
        except Exception:
            mult = 1.0
    try:
        if re.search(r'\bresistencias?\b', s, flags=re.IGNORECASE) and mult > 1.0:
            mult = mult / 10.0
    except Exception:
        pass
    return mult


def _pack_multiplier_from_description(desc: str) -> float:
    """
    Busca 'PACK xN' o 'PACK XN' en la descripción completa (case-insensitive).
    Extrae N y devuelve ese multiplicador. Si no hay match, devuelve 1.0.
    """
    try:
        s = str(desc or "")
    except Exception:
        s = ""
    if not s:
        return 1.0
    try:
        m = re.search(r'pack\s*[xX]\s*(\d+)', s, flags=re.IGNORECASE)
        if m:
            n = int(m.group(1))
            return float(max(1, n))
    except Exception:
        pass
    return 1.0


def _apply_sku_multiplier_if_present(sku_text: str, qty: int, unit_price: float) -> Tuple[str, int, float]:
    """
    SKU 'NNNNxM' o 'NNNNX M':
      - sku_base = primeras 4 alfanuméricas.
      - qty *= M
      - unit_price /= M
    Devuelve (sku_base_norm, qty_ajustada, unit_price_ajustada).
    """
    s = str(sku_text or "").strip()
    m = re.match(r'^\s*(\w{4})\s*[xX]\s*(\d+)\s*$', s)
    if m:
        base = m.group(1)
        try:
            mult = int(m.group(2))
        except Exception:
            mult = 1
        mult = max(1, mult)
        try:
            qty_adj = int(round(int(qty or 0) * mult))
        except Exception:
            qty_adj = int(qty or 0) * mult
        try:
            price_adj = float(unit_price or 0.0) / float(mult)
        except Exception:
            price_adj = float(unit_price or 0.0)
        return _norm_sku(base), qty_adj, price_adj
    return _norm_sku(s), int(qty or 0), float(unit_price or 0.0)


def _metros_multiplier_from_name(name: Any) -> float:
    """
    Si la descripción del artículo contiene 'metros' (case-insensitive),
    y hasta 4 caracteres antes hay un número (1..4 dígitos), usar ese número como multiplicador.
    Se acepta con o sin espacios: '10 metros', '10metros', ' 3   Metros', '25METROS'.
    Si no hay match, devuelve 1.0.
    Nota: La EXCEPCIÓN por SKU=5283 se aplica en el flujo de construcción de ítems.
    """
    try:
        s = str(name or "")
    except Exception:
        return 1.0
    if not s:
        return 1.0
    try:
        m = re.search(r'(\d{1,4})\s*metros\b', s, flags=re.IGNORECASE)
        if m:
            n = int(m.group(1))
            return float(max(1, n))
    except Exception:
        pass
    return 1.0


def _has_x_in_first_letters(txt: Any) -> bool:
    """True si el texto tiene una 'X' en las primeras letras, por ejemplo:
      - '10 X ...' / '10X ...' / 'X 5 ...'
    Se usa para excepciones de multiplicadores.
    """
    try:
        s = str(txt or "").strip().upper()
    except Exception:
        return False
    if not s:
        return False
    # Opcional: número al inicio, luego 'X' como token/prefijo, seguido de separador o fin.
    return re.match(r'^(?:\d+\s*)?X(?:\s|[-_/]|$)', s) is not None


# --------------------- KITS (expansión como Alta de Ventas) ---------------------

def _get_cost_for_sku(owner, sku: str) -> float:
    """Obtiene costo unitario (último costo>0 en it_comp)."""
    db = getattr(owner, "_db_path", None)
    if not db or not os.path.isfile(db):
        return 0.0
    try:
        con = sqlite3.connect(db)
        cur = con.cursor()
        cur.execute("PRAGMA table_info(it_comp)")
        cols = [r[1].lower() for r in cur.fetchall()]
        col_codigo = 'codigo' if 'codigo' in cols else ('articulo' if 'articulo' in cols else ('sku' if 'sku' in cols else 'codigo'))
        q = (
            "SELECT ic.costo FROM it_comp AS ic "
            "LEFT JOIN compras AS c ON c.remito = ic.remito "
            f"WHERE ic.{col_codigo} = ? AND ic.costo > 0 "
            "ORDER BY COALESCE(c.fecha, '' ) DESC, ic.rowid DESC "
            "LIMIT 1"
        )
        cur.execute(q, (str(sku).strip(),))
        row = cur.fetchone()
        con.close()
        return float(row[0] or 0.0) if row and row[0] is not None else 0.0
    except Exception:
        return 0.0


def _get_articulo_descrip(owner, sku: str) -> str:
    """Devuelve articulo.descrip para el SKU (fallback a SKU si no se encuentra)."""
    db = getattr(owner, "_db_path", None)
    sku_s = str(sku or "").strip()
    if not db or not os.path.isfile(db) or not sku_s:
        return sku_s
    try:
        con = sqlite3.connect(db)
        cur = con.cursor()
        for col in ("codigo", "CODIGO", "cod", "COD", "ID"):
            try:
                cur.execute(f"SELECT descrip FROM articulo WHERE COALESCE(CODIGO,codigo,ID,{col})=? LIMIT 1", (sku_s,))
                r = cur.fetchone()
                if r and r[0]:
                    con.close()
                    return str(r[0])
            except Exception:
                continue
        try:
            cur.execute("SELECT descrip FROM articulo WHERE codigo=? LIMIT 1", (sku_s,))
            r = cur.fetchone()
            if r and r[0]:
                con.close()
                return str(r[0])
        except Exception:
            pass
        con.close()
    except Exception:
        pass
    return sku_s


def _get_kit_components_from_db(owner, kit_code: str) -> List[Dict[str, Any]]:
    """Devuelve lista de componentes [{'sku','cantidad','participacion_pct'}...] del kit."""
    data_row: Optional[Dict[str, Any]] = None
    seq_row: Optional[List[Any]] = None

    try:
        import kits_armados_db as _kits_db
        _kits_get_fn = getattr(_kits_db, "get_kit", None)
    except Exception:
        _kits_get_fn = None

    dbp = getattr(owner, "_db_path", None)

    if _kits_get_fn and dbp and os.path.isfile(dbp):
        try:
            con = sqlite3.connect(dbp)
            dr = _kits_get_fn(con, kit_code)
            con.close()
            if isinstance(dr, dict):
                data_row = dr
            elif isinstance(dr, (list, tuple)):
                seq_row = list(dr)
        except Exception:
            data_row = None
            seq_row = None

    if data_row is None and dbp and os.path.isfile(dbp):
        try:
            con = sqlite3.connect(dbp)
            cur = con.cursor()
            cur.execute("SELECT * FROM kits_armados WHERE COALESCE(CODIGO,codigo,ID)=? LIMIT 1", (kit_code,))
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT * FROM kits_armados WHERE LOWER(COALESCE(CODIGO,codigo,ID))=LOWER(?) LIMIT 1", (kit_code,))
                row = cur.fetchone()
            if row is None:
                cur.execute("SELECT * FROM kits_armados WHERE COALESCE(CODIGO,codigo,ID) LIKE ? LIMIT 1", (f"%{kit_code}%",))
                row = cur.fetchone()
            if row is not None:
                cols = [c[1] for c in cur.execute("PRAGMA table_info(kits_armados)").fetchall()]
                data_row = {cols[i]: row[i] for i in range(len(cols))}
            con.close()
        except Exception:
            data_row = None

    comps: List[Dict[str, Any]] = []
    if data_row:
        for j in range(1, 31):
            sku_val = ""
            for k in (f"SKU{j}", f"sku{j}", f"SKU_{j}", f"sku_{j}"):
                if k in data_row and data_row.get(k) not in (None, ""):
                    sku_val = str(data_row.get(k)).strip()
                    break
            if not sku_val:
                continue
            cant_val = None
            for ck in (f"CANTIDAD{j}", f"cantidad{j}", f"CANT{j}", f"cant{j}", f"cantidad_{j}"):
                if ck in data_row and data_row.get(ck) not in (None, ""):
                    cant_val = data_row.get(ck)
                    break
            try:
                cant_f = int(float(cant_val)) if cant_val not in (None, "") else 0
            except Exception:
                cant_f = 0
            if cant_f <= 0:
                continue
            pct = 0.0
            for pk in (f"PARTICIPACION{j}", f"participacion{j}", f"PART{j}", f"part{j}", f"PARTICIPACION_{j}"):
                if pk in data_row and data_row.get(pk) not in (None, ""):
                    try:
                        pct = float(str(data_row.get(pk)).replace(",", "."))
                    except Exception:
                        pct = 0.0
                    break
            comps.append({"sku": sku_val, "cantidad": cant_f, "participacion_pct": float(pct or 0.0)})
        any_pct = any((c.get("participacion_pct") or 0.0) > 0.0 for c in comps)
        if not any_pct and comps:
            total_qty = sum(c["cantidad"] for c in comps) or 1
            for c in comps:
                c["participacion_pct"] = round((c["cantidad"] / total_qty) * 100.0, 6)
        return comps

    if seq_row:
        seq = list(seq_row)
        n = len(seq)
        i = 5 if n > 5 else 0
        while i < n - 1:
            a = seq[i]
            b = seq[i + 1]
            if not isinstance(a, str):
                i += 1
                continue
            s = a.strip()
            if re.fullmatch(r"\d{3,6}", s) or re.fullmatch(r"[A-Za-z0-9\-]{3,8}", s):
                cant = 0
                if isinstance(b, (int, float)):
                    try:
                        cant = int(float(b))
                    except Exception:
                        cant = 0
                else:
                    try:
                        t = str(b).strip()
                        val = float(t.replace(",", ".")) if t else 0.0
                        cant = int(val)
                    except Exception:
                        cant = 0
                if cant > 0 and cant <= 1000000:
                    comps.append({"sku": s, "cantidad": cant, "participacion_pct": 0.0})
                    i += 2
                    continue
            i += 1
        any_pct = any((c.get("participacion_pct") or 0.0) > 0.0 for c in comps)
        if not any_pct and comps:
            total_qty = sum(c["cantidad"] for c in comps) or 1
            for c in comps:
                c["participacion_pct"] = round((c["cantidad"] / total_qty) * 100.0, 6)
        return comps

    return []


def _expand_kit_items_if_needed(owner, line_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Expande SIEMPRE cualquier item cuyo SKU contenga 'kit' (case-insensitive).
    """
    expanded: List[Dict[str, Any]] = []
    for li in (line_items or []):
        sku_raw = str(li.get("sku") or li.get("articulo") or li.get("codigo") or "").strip()
        if "kit" not in sku_raw.lower():
            expanded.append(li)
            continue

        try:
            qty_kit = int(li.get("quantity") or li.get("qty") or 1)
        except Exception:
            try:
                qty_kit = int(float(str(li.get("quantity") or "1")))
            except Exception:
                qty_kit = 1
        try:
            precio_kit = _robust_parse_number(li.get("price") or li.get("unit_price") or li.get("total") or li.get("subtotal") or 0.0)
        except Exception:
            precio_kit = 0.0

        comps = _get_kit_components_from_db(owner, sku_raw)
        if not comps:
            expanded.append(li)
            continue

        all_costs = True
        comp_cost: Dict[str, float] = {}
        for comp in comps:
            sku_c = str(comp.get("sku") or "").strip()
            if not sku_c:
                comp_cost[sku_c] = 0.0
                all_costs = False
                continue
            c = _get_cost_for_sku(owner, sku_c)
            comp_cost[sku_c] = float(c or 0.0)
            if (c or 0.0) <= 0.0:
                all_costs = False

        comps_scaled = []
        for comp in comps:
            sku_c = str(comp.get("sku") or "").strip()
            base_cant = int(comp.get("cantidad") or 0)
            total_cant = base_cant * max(1, int(qty_kit))
            pct = float(comp.get("participacion_pct") or 0.0)
            comps_scaled.append({"sku": sku_c, "base_cant": base_cant, "cantidad": total_cant, "participacion_pct": pct})

        if all_costs and precio_kit > 0.0:
            total_cost_per_kit = 0.0
            for comp in comps_scaled:
                sku_c = str(comp.get("sku") or "").strip()
                base_c = int(comp.get("base_cant") or 0)
                cost_unit = float(comp_cost.get(sku_c) or 0.0)
                total_cost_per_kit += (cost_unit * base_c)
            total_margin_per_kit = float(precio_kit or 0.0) - total_cost_per_kit

            for comp in comps_scaled:
                sku_c = str(comp.get("sku") or "").strip()
                base_c = int(comp.get("base_cant") or 0)
                pct = float(comp.get("participacion_pct") or 0.0)
                cost_unit = float(comp_cost.get(sku_c) or 0.0)
                share_margin = total_margin_per_kit * (pct / 100.0)
                per_unit_margin = (share_margin / base_c) if base_c else 0.0
                precio_unit = round(cost_unit + per_unit_margin, 2)
                nombre_descrip = _get_articulo_descrip(owner, sku_c)
                expanded.append({
                    "sku": _norm_sku(sku_c), "name": nombre_descrip, "quantity": int(comp.get("cantidad") or 0),
                    "price": float(precio_unit), "subtotal": float(precio_unit) * int(comp.get("cantidad") or 0),
                    "total": float(precio_unit) * int(comp.get("cantidad") or 0),
                })
        else:
            for comp in comps_scaled:
                sku_c = str(comp.get("sku") or "").strip()
                pct = float(comp.get("participacion_pct") or 0.0)
                precio_unit = round((precio_kit * (pct / 100.0)), 2) if precio_kit and pct else 0.0
                nombre_descrip = _get_articulo_descrip(owner, sku_c)
                expanded.append({
                    "sku": _norm_sku(sku_c), "name": nombre_descrip, "quantity": int(comp.get("cantidad") or 0),
                    "price": float(precio_unit), "subtotal": float(precio_unit) * int(comp.get("cantidad") or 0),
                    "total": float(precio_unit) * int(comp.get("cantidad") or 0),
                })
    return expanded


# --------------------- SKU '+' (dividir en 2 artículos) ---------------------

def _expand_plus_skus_if_needed(owner, line_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Si un SKU contiene '+', dividir en dos líneas:
      - sku1 = antes del '+'
      - sku2 = después del '+'

    Regla (igual que en el Facturador WEB):
      - El segundo SKU se vende al costo (precio = costo_unit; subtotal = costo_unit * qty).
      - El precio de venta del primer SKU = total_original - subtotal_del_segundoSKU.

    NOTA: al segundo SKU también le seteamos 'costo' explícito para que ventas_ops lo respete.
    """
    out: List[Dict[str, Any]] = []
    for li in (line_items or []):
        sku_in = str(li.get("sku") or li.get("articulo") or li.get("codigo") or "").strip()
        if "+" not in sku_in:
            out.append(li)
            continue

        try:
            sku1, sku2 = [s.strip() for s in sku_in.split("+", 1)]
        except Exception:
            out.append(li)
            continue

        try:
            qty = int(float(li.get("quantity") or li.get("qty") or li.get("cantidad") or 0))
        except Exception:
            qty = 0
        if qty <= 0:
            out.append(li)
            continue

        total_orig = _robust_parse_number(li.get("total") if li.get("total") is not None else li.get("subtotal"))
        if total_orig <= 0.0:
            try:
                unit_price = float(li.get("price") or li.get("unit_price") or 0.0)
            except Exception:
                unit_price = 0.0
            total_orig = float(unit_price) * float(qty)

        costo2_unit = float(_get_cost_for_sku(owner, sku2) or 0.0)
        total2 = round(costo2_unit * qty, 2)

        total1 = max(0.0, round(float(total_orig) - float(total2), 2))
        price1 = round((total1 / qty), 2) if qty else 0.0

        name1 = str(li.get("title") or li.get("name") or li.get("nombre") or "").strip() or _get_articulo_descrip(owner, sku1)
        name2 = _get_articulo_descrip(owner, sku2)

        li1 = dict(li)
        li1["sku"] = sku1
        li1["name"] = name1
        li1["title"] = name1
        li1["quantity"] = qty
        li1["price"] = float(price1)
        li1["subtotal"] = float(total1)
        li1["total"] = float(total1)

        li2 = dict(li)
        li2["sku"] = sku2
        li2["name"] = name2
        li2["title"] = name2
        li2["quantity"] = qty
        li2["price"] = float(costo2_unit)
        li2["subtotal"] = float(total2)
        li2["total"] = float(total2)
        li2["costo"] = float(costo2_unit)

        out.append(li1)
        out.append(li2)

    return out


# --------------------- Clase principal ---------------------

class DataHandlersFacturarMixin:
    def _open_facturar_window(self, keys: List[str], meta: Dict[str, Dict[str, str]],
                              grouped: Dict[str, List[Dict[str, Any]]],
                              factura_cost_entries: Dict[str, tk.Entry],
                              factura_vendor_entries: Dict[str, ttk.Combobox],
                              parent: Optional[tk.Toplevel] = None):
        owner = self
        try:
            _set_ml_vendedor(_resolve_usuario_ml(owner))
        except Exception:
            pass

        win = tk.Toplevel(parent if parent else None)
        win.title("Facturación — Detalle por factura")
        try:
            win.state("zoomed")
        except Exception:
            try:
                win.attributes("-zoomed", True)
            except Exception:
                win.geometry("1600x900+0+0")

        header = tk.Frame(win, bg="#f3f4f6")
        header.pack(fill="x", padx=8, pady=(6, 0))
        lbl_ok = tk.Label(header, text="OK: 0", font=("Segoe UI", 12, "bold"), fg="#16a34a", bg="#f3f4f6")
        lbl_ok.pack(side="left", padx=(0, 12))
        lbl_err = tk.Label(header, text="ERROR: 0", font=("Segoe UI", 12, "bold"), fg="#b91c1c", bg="#f3f4f6")

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        fact_style = "Facturar.Treeview"
        style.layout(fact_style, style.layout("Treeview"))
        style.configure(fact_style, rowheight=24)
        style.configure("Treeview.Heading", background="#000000", foreground="#ffffff", font=("Segoe UI", 10, "bold"))
        style.map("Treeview.Heading", background=[("active", "#000000")], foreground=[("active", "#ffffff")])

        def _tracking_from_order(o: Dict[str, Any]) -> str:
            try:
                sh_raw = o.get("shipment_raw") or {}
                if isinstance(sh_raw, dict) and sh_raw.get("tracking_number"):
                    return str(sh_raw.get("tracking_number"))
                shp = o.get("shipping") or {}
                tn2 = (shp.get("tracking_number") or shp.get("tracking"))
                return str(tn2) if tn2 else ""
            except Exception:
                return ""

        tabs_state: Dict[tk.Frame, bool] = {}

        def _refresh_ok_err_and_button():
            ok_count = sum(1 for v in tabs_state.values() if v)
            err_count = sum(1 for v in tabs_state.values() if not v)
            lbl_ok.configure(text=f"OK: {ok_count}")
            lbl_err.configure(text=f"ERROR: {err_count}")
            try:
                if err_count == 0:
                    if lbl_err.winfo_ismapped():
                        lbl_err.pack_forget()
                else:
                    if not lbl_err.winfo_ismapped():
                        lbl_err.pack(side="left")
            except Exception:
                pass
            try:
                btn_generar.configure(state="normal" if ok_count > 0 else "disabled")
            except Exception:
                pass

        for k in keys:
            info = meta.get(k) or {}
            base_title = f"{info.get('kind','FACT')} {info.get('id','')}"
            frm = tk.Frame(nb, bg="#ffffff")
            nb.add(frm, text=base_title)

            hdr = tk.Frame(frm, bg="#ffffff")
            hdr.pack(fill="x", padx=8, pady=(6, 4))
            tk.Label(hdr, text=f"Factura: {base_title}", font=("Segoe UI", 12, "bold"), bg="#ffffff", fg="#000000").pack(side="left")

            frm_ctrl = tk.Frame(hdr, bg="#ffffff")
            frm_ctrl.pack(side="right")

            orders_group = grouped.get(k, []) or []

            # Nuestro costo de envío (traído de PREVIEW)
            try:
                frm._nuestro_costo_envio = float(_robust_parse_number((meta.get(k, {}) or {}).get("nuestro_costo_envio")) or 0.0)
            except Exception:
                frm._nuestro_costo_envio = 0.0

            # Ajustar shipping_cost_seller para MERCADO ENVIOS según shipment.shipping_option.list_cost
            try:
                for o in orders_group:
                    if isinstance(o, dict) and (o.get("tipo_envio") or "").strip().upper() == "MERCADO ENVIOS":
                        corrected = _infer_envio_seller_for_order(o)
                        if corrected and corrected > 0:
                            o["shipping_cost_seller"] = float(corrected)
            except Exception:
                pass

            # Ocultar controles cuando TODAS las órdenes son retiro (no costo) o MERCADO ENVIOS (costo autocalculado)
            is_pickup_group = False
            is_me_group = False
            me_cost_val = 0.0
            try:
                tipos = [_extract_tipo_envio(o) for o in orders_group]
                is_pickup_group = (len(tipos) > 0 and all(_is_pickup_tipo_envio(t) for t in tipos))
                is_me_group = (len(tipos) > 0 and all((t or '').strip().upper() in {'MERCADO ENVIOS', 'MERCADO ENVÍOS'} for t in tipos))
            except Exception:
                is_pickup_group = False
                is_me_group = False

            # Para MERCADO ENVIOS, autocalcular costo de envío vendedor
            if is_me_group:
                try:
                    for _o in orders_group:
                        v = _infer_envio_seller_for_order(_o)
                        if v and float(v) != 0.0:
                            me_cost_val = float(v)
                            break
                except Exception:
                    pass

            cbo_vendor = None
            ent_cost = None
            has_envio_item = False
            try:
                has_envio_item = _group_has_envio_item(orders_group)
            except Exception:
                has_envio_item = False
            frm._is_me_group = bool(is_me_group)
            frm._has_envio_item = bool(has_envio_item)

            if not (is_pickup_group or is_me_group):
                tk.Label(frm_ctrl, text="Venta despachada por:", bg="#ffffff", fg="#000000").pack(side="left", padx=(0, 6))
                vendor_val = factura_vendor_entries.get(k).get() if factura_vendor_entries.get(k) else ""
                cbo_vendor = ttk.Combobox(frm_ctrl, state="readonly", values=["Pato", "Jhonatan", "NSA"], width=18)
                cbo_vendor.set(vendor_val or "Pato")
                cbo_vendor.pack(side="left", padx=(0, 12))

                tk.Label(frm_ctrl, text="Costo de envío de esta Factura:", bg="#ffffff", fg="#000000").pack(side="left", padx=(0, 6))
                cost_val = factura_cost_entries.get(k).get() if factura_cost_entries.get(k) else ""
                ent_cost = tk.Entry(frm_ctrl, width=12)
                ent_cost.insert(0, cost_val or "")
                ent_cost.configure(state="readonly")
                ent_cost.pack(side="left")

                factura_vendor_entries[k] = cbo_vendor
                factura_cost_entries[k] = ent_cost
                frm._default_vendor_name = "Pato"
                frm._fact_cost_value = 0.0
            else:
                try:
                    factura_vendor_entries.pop(k, None)
                except Exception:
                    pass
                try:
                    factura_cost_entries.pop(k, None)
                except Exception:
                    pass
                if is_me_group:
                    frm._default_vendor_name = "NSA"
                    try:
                        _nc = float(getattr(frm, "_nuestro_costo_envio", 0.0) or 0.0)
                        if _nc > 0.0:
                            me_cost_val = float(_nc)
                    except Exception:
                        pass
                    frm._fact_cost_value = float(me_cost_val or 0.0)
                else:
                    frm._default_vendor_name = "Pato"
                    frm._fact_cost_value = 0.0

            # Detectar FLEX
            is_group_flex = False
            try:
                is_group_flex = any((_extract_tipo_envio(o).strip().upper() == "FLEX") for o in orders_group)
            except Exception:
                is_group_flex = False

            # Detectar RETIRO/ACUERDO (no requiere costo de envío)
            try:
                tipos_raw = [_extract_tipo_envio(o) for o in orders_group]
                tipos_raw = [t for t in tipos_raw if str(t).strip()]
                is_group_pickup = bool(tipos_raw) and all(_is_pickup_tipo_envio(t) for t in tipos_raw)
            except Exception:
                is_group_pickup = False
            frm._requires_cost = (not bool(is_group_pickup)) and not (bool(getattr(frm, "_is_me_group", False)) and not bool(getattr(frm, "_has_envio_item", False)))

            # ---------------- Tabla de Pagos (height=12) ----------------
            mp_frame = tk.Frame(frm, bg="#ffffff")
            mp_frame.pack(fill="x", padx=8, pady=(4, 8))
            cols_pay = ("pago_id", "orden", "pack", "fecha", "estado", "monto", "neto", "envio_pago", "metodo", "tipo", "cuotas", "ult4")
            tv_pay = ttk.Treeview(mp_frame, columns=cols_pay, show="headings", height=12, style=fact_style)
            headers_pay = {
                "pago_id": "Pago ID", "orden": "Orden", "pack": "Pack", "fecha": "Fecha (AR -03)", "estado": "Estado",
                "monto": "Monto", "neto": "Neto Recibido", "envio_pago": "Env. Pago", "metodo": "Método",
                "tipo": "Tipo", "cuotas": "Cuotas", "ult4": "Últimos 4"
            }
            widths_pay = {"pago_id": 160, "orden": 160, "pack": 140, "fecha": 180, "estado": 110, "monto": 110, "neto": 130,
                          "envio_pago": 120, "metodo": 120, "tipo": 120, "cuotas": 70, "ult4": 90}
            for c in cols_pay:
                tv_pay.heading(c, text=headers_pay[c])
                tv_pay.column(
                    c,
                    width=widths_pay[c],
                    anchor=("e" if c in {"monto", "neto", "envio_pago"} else
                            "center" if c in {"pago_id", "orden", "pack", "estado", "metodo", "tipo", "cuotas", "ult4"} else "w"),
                    stretch=False
                )
            tv_pay.tag_configure("total", background="#FFF2CC")
            tv_pay.pack(fill="x", padx=0, pady=(2, 4))

            pagos_all_visible: List[Dict[str, Any]] = []
            pagos_all_sum: List[Dict[str, float]] = []
            sum_envio_pago_raw = 0.0
            seen_pay_ids = set()
            neto_modificado_flag = False
            has_cuotas_gt1 = False  # si algún Pago ID tiene cuotas>1

            mp_by_id = {str(r.get("mp_id")): r for r in (getattr(owner, "_filas_mp_full", None) or [])}
            mp_tracking_index = getattr(owner, "_mp_tracking_index", None) or {}
            group_order_ids = {str(o.get("order_id") or "") for o in orders_group}
            try:
                group_pack_id = str(next((o.get("pack_id") for o in orders_group if o.get("pack_id")), "")) or ""
            except Exception:
                group_pack_id = ""

            # Pagos ML (+ regla FLEX para neto) — OMITIR rejected
            for o in orders_group:
                orden_id = str(o.get("order_id") or "")
                for p in (o.get("payments_raw") or []):
                    try:
                        status = str(p.get("status") or p.get("status_detail") or "").strip().lower()
                    except Exception:
                        status = ""
                    # Omitir pagos rechazados / cancelados
                    if _is_skippable_payment_status(status):
                        continue

                    pid_s = str(p.get("id") or "")
                    if pid_s:
                        if pid_s in seen_pay_ids:
                            continue
                        seen_pay_ids.add(pid_s)

                    fecha_dt = self._parse_fecha_ml(p.get("date_approved") or p.get("date_created"))
                    td = p.get("transaction_details") or {}
                    ship_c = p.get("shipping_cost")
                    if ship_c is None:
                        ship_c = td.get("shipping_cost")

                    monto_val = float(p.get("transaction_amount") or p.get("amount") or 0.0)
                    neto_val = float(p.get("net_received_amount") or p.get("total_paid_amount") or monto_val)

                    if is_group_flex and (monto_val > 33000.0) and abs(neto_val - monto_val) > 0.009:
                        neto_val = monto_val
                        neto_modificado_flag = True

                    try:
                        sum_envio_pago_raw += float(ship_c or 0.0)
                    except Exception:
                        pass

                    # Cuotas (installments): si hay cuotas > 1, el chequeo de totales usa MONTO en vez de NETO
                    try:
                        inst = int(p.get("installments") or 0)
                    except Exception:
                        inst = 0
                    if inst > 1:
                        has_cuotas_gt1 = True

                    tv_pay.insert("", "end", values=(
                        pid_s, orden_id, group_pack_id, self._fmt_dt_ar(fecha_dt), p.get("status") or "",
                        self._fmt_money(monto_val), self._fmt_money(neto_val), self._fmt_money(ship_c),
                        p.get("payment_method_id") or "", p.get("payment_type") or p.get("payment_type_id") or "",
                        inst if inst else "", (p.get("card") or {}).get("last_four_digits") or (p.get("card_holder") or {}).get("last_four") or ""
                    ))
                    pagos_all_sum.append({"monto": monto_val, "neto": neto_val})
                    pagos_all_visible.append({"pago_id": pid_s, "orden": orden_id, "monto": monto_val, "neto": neto_val, "cuotas": inst, "pack": group_pack_id})

            # Pagos por tracking (MP Full) — OMITIR rejected
            tracking_payments_for_envio: List[float] = []
            for o in orders_group:
                tr = _tracking_from_order(o)
                if not tr:
                    continue
                mp_ids = mp_tracking_index.get(str(tr), []) or []
                for mpid in mp_ids:
                    mpid_s = str(mpid)
                    if mpid_s in seen_pay_ids:
                        continue
                    rmp = mp_by_id.get(mpid_s) or next((r for r in (getattr(owner, "_filas_mp_full", None) or []) if str(r.get("mp_id")) == mpid_s), None)
                    if not rmp:
                        continue
                    status_mp = str(rmp.get("status") or "").strip().lower()
                    # Omitir pagos rechazados / cancelados
                    if _is_skippable_payment_status(status_mp):
                        continue

                    fecha_dt = rmp.get("fecha_dt")
                    monto_val = float(rmp.get("amount") or 0.0)
                    neto_val = monto_val
                    order_for_mp_s = str(rmp.get("operation_related") or rmp.get("order_id") or "")

                    tv_pay.insert("", "end", values=(
                        mpid_s, order_for_mp_s, group_pack_id, self._fmt_dt_ar(fecha_dt) if fecha_dt else "", rmp.get("status") or "",
                        self._fmt_money(monto_val), self._fmt_money(neto_val), self._fmt_money(0.0),
                        rmp.get("method") or "", rmp.get("operation_type") or "", "", ""
                    ))
                    pagos_all_sum.append({"monto": monto_val, "neto": neto_val})
                    pagos_all_visible.append({"pago_id": mpid_s, "orden": order_for_mp_s, "neto": neto_val, "pack": group_pack_id})
                    if not order_for_mp_s or order_for_mp_s not in group_order_ids:
                        tracking_payments_for_envio.append(monto_val)
                    seen_pay_ids.add(mpid_s)

            sum_netos_all = sum((p.get("neto") or 0.0) for p in pagos_all_sum)
            sum_montos_all = sum((p.get("monto") or 0.0) for p in pagos_all_sum)
            frm._sum_netos_all = float(sum_netos_all)
            frm._sum_montos_all = float(sum_montos_all)
            frm._sum_envio_pago_raw = float(sum_envio_pago_raw or 0.0)  # guardar para ajustes posteriores
            frm._has_cuotas_gt1 = bool(has_cuotas_gt1)

            tv_pay.insert("", "end",
                          values=("TOTAL", "", "", "", "", self._fmt_money(sum_montos_all), self._fmt_money(sum_netos_all), "", "", "", ""),
                          tags=("total",))

            if neto_modificado_flag:
                tk.Label(frm, text="Neto recibido modificado", fg="#ff0000", bg="#ffffff", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=18)

            # ---------------- Tabla de Órdenes del grupo ----------------
            cols_orders = ("order_id", "pack", "fecha", "cliente", "total_neto", "tracking")
            tv_orders = ttk.Treeview(frm, columns=cols_orders, show="headings", height=4, style=fact_style)
            specs_orders = {
                "order_id": ("Orden", 180, "w"),
                "pack": ("Pack", 160, "center"),
                "fecha": ("Fecha", 260, "w"),
                "cliente": ("Cliente", 260, "w"),
                "total_neto": ("Total (NETO)", 160, "e"),
                "tracking": ("Tracking", 180, "w"),
            }
            for c in cols_orders:
                txt, w, a = specs_orders[c]
                tv_orders.heading(c, text=txt)
                tv_orders.column(c, width=w, anchor=a, stretch=False)
            orders_row = tk.Frame(frm, bg="#ffffff")
            orders_row.pack(fill="x", padx=8, pady=(6, 8))
            tv_orders.pack(in_=orders_row, side="left", fill="x", expand=True)
            info_right = tk.Frame(orders_row, bg="#ffffff")
            info_right.pack(side="right", padx=(14, 0))
            lbl_nuestro = tk.Label(info_right, text="", font=("Segoe UI", 11, "bold"), bg="#ffffff", fg="#000000", justify="right")
            lbl_nuestro.pack(anchor="e")
            frm._lbl_nuestro_costo_envio = lbl_nuestro
            try:
                _nc = float(getattr(frm, "_nuestro_costo_envio", 0.0) or 0.0)
            except Exception:
                _nc = 0.0
            if _nc > 0.0:
                lbl_nuestro.config(text=f"Nuestro costo de envío: {self._fmt_money(_nc)}")
            else:
                lbl_nuestro.config(text="")

            pack_unique = group_pack_id
            first_row = True
            for o in sorted(orders_group, key=lambda x: self._parse_fecha_ml(x.get("date_created")), reverse=True):
                fecha = o.get("date_created") or o.get("date") or ""
                cliente = o.get("buyer_nickname") or o.get("buyer") or ""
                total = float(o.get("total_pagado_aprobado") or o.get("total") or 0.0)
                tracking = _tracking_from_order(o)
                tv_orders.insert("", "end", values=(
                    str(o.get("order_id") or ""),
                    pack_unique if first_row else "",
                    str(fecha), str(cliente), self._fmt_money(total), str(tracking)
                ))
                first_row = False

            # ---------------- Armado de ítems (KITS + multiplicadores + METROS) ----------------
            combined_items: List[Dict[str, Any]] = []
            # Componentes de envío (se normalizan más abajo al fijar el ítem 6696)
            frm._envio_visual_amount = 0.0
            # Flag: si 6696 fue agregado desde _envio_visual_amount (para evitar doble conteo)
            frm._envio_6696_from_envio_visual = False
            for o in orders_group:
                raw_items = (o.get("line_items") or o.get("productos") or [])
                expanded_if_kit = _expand_kit_items_if_needed(owner, raw_items)
                expanded_if_plus = _expand_plus_skus_if_needed(owner, expanded_if_kit)
                for li in expanded_if_plus:
                    sku_raw = li.get("sku") or li.get("articulo") or li.get("codigo")
                    sku_base = _norm_sku(sku_raw)
                    nombre_original = str(li.get("title") or li.get("name") or li.get("nombre") or "")
                    nombre = nombre_original or _get_articulo_descrip(owner, sku_base)
                    try:
                        qty_base = int(li.get("quantity") or li.get("qty") or 0)
                    except Exception:
                        try:
                            qty_base = int(float(str(li.get("quantity") or "0")))
                        except Exception:
                            qty_base = 0

                    subtotal_pref = li.get("total") if li.get("total") is not None else (li.get("subtotal") if li.get("subtotal") is not None else None)
                    if subtotal_pref is None:
                        try:
                            unit_price_orig = float(li.get("price") or li.get("unit_price") or 0.0)
                            subtotal_pref = unit_price_orig * (qty_base if qty_base else 0)
                        except Exception:
                            unit_price_orig = 0.0
                            subtotal_pref = 0.0
                    try:
                        unit_price_orig = float(li.get("price") or li.get("unit_price") or (float(subtotal_pref) / (qty_base if qty_base else 1)))
                    except Exception:
                        unit_price_orig = 0.0

                    # Aplicar multiplicadores con excepciones:
                    sku_base2, qty_after_sku_mult, unit_price_after_sku_mult = _apply_sku_multiplier_if_present(str(sku_raw or ""), qty_base, unit_price_orig)
                    sku_norm2 = _norm_sku(sku_base2)
                    only_sku_x = (sku_norm2 == _norm_sku("5293"))
                    skip_all_mult = _sku_skip_rule(sku_norm2, "MULT")

                    pack_mult_name = _pack_multiplier_from_name(nombre)
                    
                    # Descripción del ítem (si está disponible). Si no viene, se cae al título.
                    desc_txt = str(li.get("description") or li.get("descripcion") or li.get("desc") or "")
                    
                    # Multiplicador por descripción: usa la descripción real, no el título
                    pack_mult_desc = _pack_multiplier_from_description(desc_txt or nombre)
                    
                    # EXCEPCION: SKU 6404 => NO aplicar multiplicador 'PACK XN' desde descripcion/titulo
                    if sku_norm2 == _norm_sku("6404"):
                        pack_mult_desc = 1.0
                    if sku_norm2 == _norm_sku("6405"):
                        pack_mult_desc = 1.0
                    # Excepción pedida: para 1637/1640, si TÍTULO o DESCRIPCIÓN tiene una 'X' en las primeras letras,
                    # no aplicar multiplicadores automáticos por nombre/desc/metros.
                    x_first = (_has_x_in_first_letters(nombre) or _has_x_in_first_letters(desc_txt))
                    if sku_norm2 in {_norm_sku("1637"),_norm_sku("1640"),_norm_sku("1644"), _norm_sku("1341"), _norm_sku("1647")} and x_first:
                        pack_mult_name = 1.0
                        pack_mult_desc = 1.0
                    if _sku_skip_rule(sku_norm2, "PACK") or skip_all_mult or only_sku_x:
                        pack_mult_name = 1.0
                        pack_mult_desc = 1.0
                    pack_mult = pack_mult_name * pack_mult_desc
                    metros_mult = 1.0 if (sku_norm2 == _norm_sku("5283") or skip_all_mult or only_sku_x or (sku_norm2 in {_norm_sku("1637"),_norm_sku("1640"),_norm_sku("1644"), _norm_sku("1341"), _norm_sku("1647")} and x_first)) else _metros_multiplier_from_name(nombre)

                    qty_final = int(round(qty_after_sku_mult * pack_mult * metros_mult))
                    div_mult = (pack_mult * metros_mult) if (pack_mult * metros_mult) > 0 else 1.0
                    unit_price_final = unit_price_after_sku_mult / div_mult

                    combined_items.append({
                        "sku": sku_norm2, "nombre": nombre, "cantidad": qty_final,
                        "precio": unit_price_final, "subtotal": float(unit_price_final * qty_final)
                    })
            # ---------------- Normalización de envío (6696) ----------------
            # Hay casos donde el envío viene:
            # - como "Env. Pago" (shipping_amount) dentro del pago,
            # - y/o como un pago separado (tracking MP Full) o incluso como ítem 6696 ya presente.
            # Regla: 6696 debe reflejar el TOTAL pagado por envío por el cliente.
            frm._envio_6696_fixed = False
            frm._envio_6696_from_envio_visual = False
            try:
                tol_env = 0.50

                # 1) Si ya vino 6696 en los ítems, lo tomamos como "base" y lo removemos para dejar uno solo.
                base_6696 = 0.0
                _tmp_items = []
                for it in combined_items:
                    if _norm_sku(it.get("sku")) == _norm_sku("6696"):
                        try:
                            base_6696 += float(it.get("subtotal") or 0.0)
                        except Exception:
                            pass
                        continue
                    _tmp_items.append(it)
                combined_items[:] = _tmp_items

                # 2) Componentes externos al detalle
                sum_tracking_env = float(sum(tracking_payments_for_envio) or 0.0)
                sum_env_pago = float(sum_envio_pago_raw or 0.0)

                # Evitar doble conteo típico:
                # - tracking replica exactamente el Env.Pago
                # - tracking replica exactamente el 6696 base
                if (sum_tracking_env > 0.0 and sum_env_pago > 0.0) and (abs(sum_tracking_env - sum_env_pago) <= tol_env):
                    sum_tracking_env = 0.0
                if (sum_tracking_env > 0.0 and base_6696 > 0.0) and (abs(sum_tracking_env - base_6696) <= tol_env):
                    sum_tracking_env = 0.0

                total_envio = float(base_6696 + sum_env_pago + sum_tracking_env)
                frm._envio_visual_amount = float(total_envio or 0.0)

                # 3) Insertar un único 6696 (solo no-ME)
                if (total_envio > 0.0) and (not bool(is_me_group)):
                    combined_items.append({
                        "sku": _norm_sku("6696"),
                        "nombre": "Envio por moto flex",
                        "cantidad": 1,
                        "precio": float(total_envio),
                        "subtotal": float(total_envio),
                    })
                    frm._envio_6696_from_envio_visual = True
                    frm._envio_6696_fixed = True
            except Exception:
                pass


            # NUEVO: MERCADO ENVIOS -> reglas 6696/6711 según monto (<33k) y neto==sin envío
            try:
                _nc = float(getattr(frm, "_nuestro_costo_envio", 0.0) or 0.0)
            except Exception:
                _nc = 0.0
            try:
                _neto_ref = float(getattr(frm, "_sum_netos_all", 0.0) or 0.0)
            except Exception:
                _neto_ref = 0.0

            me_remove_6711 = False
            me_precio_6711 = 0.0
            me_sum_sin_envio = 0.0
            me_bonificacion_ml = 0.0
            me_add_6756 = False

            # CASO ESPECIAL: MERCADO ENVIOS SIN ENVIO (Env. Cliente=0 y Env. Vendedor=0) => permitir facturar sin costo
            # (nadie paga envío, y el detalle no debe exigir costo ni insertar 6711)
            frm._me_no_shipping = False
            if bool(is_me_group) and float(_nc or 0.0) <= 0.0:
                try:
                    env_cli_total = sum(float((o or {}).get('shipping_cost') or (o or {}).get('shipping_cost_buyer') or 0.0)
                                       for o in (orders_group or []) if isinstance(o, dict))
                    env_vend_total = sum(float((o or {}).get('shipping_cost_seller') or 0.0)
                                        for o in (orders_group or []) if isinstance(o, dict))
                except Exception:
                    env_cli_total, env_vend_total = 0.0, 0.0
                try:
                    has_envio_line = any(
                        (_norm_sku((_it or {}).get('sku')) in (_norm_sku('6711'), _norm_sku('6696'), _norm_sku('6756')))
                        or ('ENVIO' in str((_it or {}).get('nombre') or '').upper())
                        for _it in (combined_items or [])
                    )
                except Exception:
                    has_envio_line = False
                if abs(float(env_cli_total or 0.0)) <= 0.0001 and abs(float(env_vend_total or 0.0)) <= 0.0001 and (not has_envio_line):
                    frm._me_no_shipping = True
                    me_remove_6711 = True
                    me_precio_6711 = 0.0
                    try:
                        me_sum_sin_envio = float(sum((it.get('subtotal') or 0.0) for it in (combined_items or [])
                                                     if _norm_sku(it.get('sku')) not in (_norm_sku('6696'), _norm_sku('6711'), _norm_sku('6756'))))
                    except Exception:
                        me_sum_sin_envio = 0.0

            if bool(is_me_group) and _nc > 0.0:
                me_sum_sin_envio = float(sum((it.get("subtotal") or 0.0) for it in combined_items
                                             if _norm_sku(it.get("sku")) not in (_norm_sku("6696"), _norm_sku("6711"), _norm_sku("6756"))))
                if me_sum_sin_envio < 33000.0 and abs(me_sum_sin_envio - float(_neto_ref or 0.0)) <= 0.50:
                    me_remove_6711 = True
                me_precio_6711 = float(_nc) if (me_sum_sin_envio < 33000.0) else 0.0

                # Quitar 6696/6711/6756 existentes del detalle (para evitar doble conteo)
                _filtered = []
                for _it in (combined_items or []):
                    _sku = _norm_sku(_it.get("sku"))
                    if _sku in (_norm_sku("6696"), _norm_sku("6711"), _norm_sku("6756")):
                        continue
                    _filtered.append(_it)
                combined_items = _filtered

                # Si corresponde, reinsertar 6711 con precio según regla
                if not me_remove_6711:
                    combined_items.append({
                        "sku": _norm_sku("6711"),
                        "nombre": "Envio por Mercado Envios",
                        "cantidad": 1,
                        "precio": float(me_precio_6711),
                        "subtotal": float(me_precio_6711),
                        "nuestro_costo_envio": float(_nc),
                    })

                # NUEVO: SKU 6756 para bonificación de MercadoLibre (envío compartido)
                # Detectar si hay bonificación: shipping_cost_seller > nuestro_costo_envio
                try:
                    env_vend_total = sum(float((o or {}).get('shipping_cost_seller') or 0.0)
                                        for o in (orders_group or []) if isinstance(o, dict))
                    if env_vend_total > 0.0 and _nc > 0.0:
                        me_bonificacion_ml = float(env_vend_total - _nc)
                        if me_bonificacion_ml > 0.01:  # Tolerancia para evitar errores de redondeo
                            me_add_6756 = True
                except Exception:
                    me_bonificacion_ml = 0.0
                    me_add_6756 = False

                # Si hay bonificación, agregar SKU 6756
                if me_add_6756:
                    combined_items.append({
                        "sku": _norm_sku("6756"),
                        "nombre": "Envio Bonificacion de MercadoLibre",
                        "cantidad": 1,
                        "precio": float(me_bonificacion_ml),
                        "subtotal": float(me_bonificacion_ml),
                        "nuestro_costo_envio": 0.0,
                    })

            # Guardar flags ME para el proceso de GENERAR (precompra/venta)
            frm._wf_me_remove_6711 = bool(me_remove_6711)
            frm._wf_me_precio_6711 = float(me_precio_6711 or 0.0)
            frm._wf_me_sum_sin_envio = float(me_sum_sin_envio or 0.0)
            frm._wf_me_bonificacion_ml = float(me_bonificacion_ml or 0.0)
            frm._wf_me_add_6756 = bool(me_add_6756)
            # Para compatibilidad: considerar "hay envio" sólo si 6711 quedó en detalle
            frm._has_envio_item = bool(is_me_group) and (not bool(me_remove_6711))

            # --------- AJUSTE POR "Env. Pago" CUANDO NETO != TOTAL (sólo no-ME) ----------
            try:
                tol_chk = 0.50
                sum_detalle_pre = float(sum((it.get("subtotal") or 0.0) for it in combined_items))
                use_monto_ref = bool(getattr(frm, "_has_cuotas_gt1", False))
                sum_ref = float(getattr(frm, "_sum_montos_all" if use_monto_ref else "_sum_netos_all", 0.0) or 0.0)
                env_pago_total = float(getattr(frm, "_sum_envio_pago_raw", 0.0) or 0.0)
                # Importante: en cuotas>1 NO ajustar 6696 por diferencias NETO vs TOTAL (son comisiones/financiación)
                if (abs(sum_ref - sum_detalle_pre) > tol_chk) and (not bool(is_me_group)) and (env_pago_total > 0.0) and (not use_monto_ref) and (not bool(getattr(frm, "_envio_6696_from_envio_visual", False))) and (not bool(getattr(frm, "_envio_6696_fixed", False))):
                    # Sumar "Env. Pago" al precio de 6696 (o agregar 6696 si no existe)
                    adjusted = False
                    for it in combined_items:
                        if _norm_sku(it.get("sku")) == _norm_sku("6696"):
                            q = int(it.get("cantidad") or it.get("quantity") or 1)
                            base_unit = float(it.get("precio") or it.get("price") or 0.0)
                            new_unit = base_unit + env_pago_total
                            it["precio"] = float(new_unit)
                            it["price"] = float(new_unit)
                            it["subtotal"] = float(new_unit * max(1, q))
                            adjusted = True
                            break
                    if not adjusted:
                        combined_items.append({
                            "sku": _norm_sku("6696"),
                            "nombre": "Envio por moto flex",
                            "cantidad": 1,
                            "precio": float(env_pago_total),
                            "subtotal": float(env_pago_total),
                        })
                        adjusted = True
                    # Actualizar valor visual y guardarlo para generación
                    if adjusted:
                        try:
                            frm._envio_visual_amount = float((frm._envio_visual_amount or 0.0) + env_pago_total)
                        except Exception:
                            frm._envio_visual_amount = float(env_pago_total or 0.0)
            except Exception:
                pass

            # BLINDAJE: guardar el detalle ya normalizado (lo que ves en la grilla)
            try:
                frm._wf_combined_items = [dict(ci) for ci in (combined_items or [])]
            except Exception:
                frm._wf_combined_items = combined_items

            skus = sorted({ci.get("sku") for ci in combined_items if ci.get("sku")})
            stock_map = owner._stock_map_for_skus(skus)
            stock_map[_norm_sku("6696")] = max(1, int(stock_map.get(_norm_sku("6696")) or 0))
            stock_map[_norm_sku("6711")] = max(1, int(stock_map.get(_norm_sku("6711")) or 0))
            stock_map[_norm_sku("6756")] = max(1, int(stock_map.get(_norm_sku("6756")) or 0))

            # ----------------- Validaciones de stock/SKU -----------------
            is_ok_tab = True
            validation_reasons: List[str] = []
            for it in combined_items:
                sku_it = str(it.get("sku") or "").strip()
                if not sku_it:
                    is_ok_tab = False
                    validation_reasons.append(f"Linea sin SKU: '{it.get('nombre') or ''}'")

            for it in combined_items:
                sku_it = str(it.get("sku") or "").strip()
                if not sku_it:
                    continue
                sku_norm = _norm_sku(sku_it)

                # No validar stock para líneas de envío
                if sku_norm in (_norm_sku("6696"), _norm_sku("6711"), _norm_sku("6756")):
                    continue

                try:
                    qty_req = int(it.get("cantidad") or it.get("quantity") or it.get("qty") or 0)
                except Exception:
                    try:
                        qty_req = int(float(str(it.get("cantidad") or it.get("quantity") or it.get("qty") or "0")))
                    except Exception:
                        qty_req = 0

                stock_val = int(stock_map.get(sku_norm) or 0)

                # Regla correcta: si pide más de lo que hay en stock => ERROR
                if stock_val < qty_req:
                    is_ok_tab = False
                    validation_reasons.append(
                        f"SKU {sku_norm} sin stock suficiente: pedido {qty_req} / stock {stock_val} (producto: '{it.get('nombre') or ''}')"
                    )

            if not combined_items and orders_group:
                is_ok_tab = False
                validation_reasons.append("Detalle vacío (sin líneas de ítems).")

            tabs_state[frm] = is_ok_tab
            try:
                nb.tab(frm, text=(base_title if is_ok_tab else f"ERROR — {base_title}"))
            except Exception:
                pass

            if not is_ok_tab:
                try:
                    warn_txt = " / ".join(validation_reasons[:5]) if validation_reasons else "SKU vacío o stock insuficiente en alguna línea."
                    lbl_warn = tk.Label(frm, text=f"ERROR: {warn_txt}", fg="#b91c1c", bg="#ffffff", font=("Segoe UI", 11, "bold"))
                    lbl_warn.pack(anchor="w", padx=18, pady=(4, 2))
                    frm._fact_validation_reasons = validation_reasons
                except Exception:
                    pass

            items_frame = tk.Frame(frm, bg="#ffffff")
            items_frame.pack(fill="both", expand=True, padx=8, pady=8)
            cols_items = ("sku", "nombre", "cant", "stock", "nuestro_costo_envio", "precio", "subtotal")
            tv_items = ttk.Treeview(items_frame, columns=cols_items, show="headings", height=12, style=fact_style)
            self.tree = tv_items
            tv_items.tag_configure("stock_faltante", background="#FECACA", foreground="#991B1B")
            specs_items = [("sku", "SKU", 120, "center"), ("nombre", "NOMBRE", 640, "w"),
                           ("cant", "CANT", 80, "center"), ("stock", "STOCK", 100, "center"),
                           ("nuestro_costo_envio", "NUESTRO COSTO ENVÍO", 180, "e"),
                           ("precio", "PRECIO", 140, "e"), ("subtotal", "SUBTOTAL", 140, "e")]
            for c, t, w, a in specs_items:
                tv_items.heading(c, text=t)
                tv_items.column(c, width=w, anchor=a, stretch=False)
            vs_items = ttk.Scrollbar(items_frame, orient="vertical", command=tv_items.yview)
            tv_items.configure(yscroll=vs_items.set)
            tv_items.pack(side="left", fill="both", expand=True)
            vs_items.pack(side="right", fill="y")

            total_items_sum = 0.0
            for it in combined_items:
                sku_display = it.get("sku") or ""
                sku_norm = _norm_sku(sku_display) if sku_display else ""
                try:
                    qty_req = int(it.get("cantidad") or it.get("quantity") or it.get("qty") or 0)
                except Exception:
                    try:
                        qty_req = int(float(str(it.get("cantidad") or it.get("quantity") or it.get("qty") or "0")))
                    except Exception:
                        qty_req = 0

                stock_val = int(stock_map.get(sku_norm) or 0) if sku_norm else 0
                stock_display = str(stock_val)

                tags = ()
                if sku_norm and sku_norm not in (_norm_sku("6696"), _norm_sku("6711"), _norm_sku("6756")) and stock_val < qty_req:
                    tags = ("stock_faltante",)

                tv_items.insert(
                    "", "end",
                    values=(
                        sku_display,
                        it.get("nombre") or "",
                        str(it.get("cantidad") or 0),
                        stock_display,
                        (self._fmt_money(float(it.get("nuestro_costo_envio") or 0.0))
                         if float(it.get("nuestro_costo_envio") or 0.0) > 0.0 else ""),
                        self._fmt_money(it.get("precio")),
                        self._fmt_money(float(it.get("subtotal") or 0.0))
                    ),
                    tags=tags
                )
                total_items_sum += float(it.get("subtotal") or 0.0)

            foot_frame = tk.Frame(frm, bg="#ffffff")
            foot_frame.pack(fill="x", padx=8, pady=(4, 8))
            tk.Label(foot_frame, text=f"Total del pedido: {self._fmt_money(total_items_sum)}",
                     font=("Segoe UI", 10, "bold"), bg="#ffffff", fg="#000000").pack(side="right")

            # Chequeo de totales:
            # - Normal: comparar TOTAL factura vs SUMA NETO recibido (tol 0.50)
            # - Excepción: si algún Pago ID tiene cuotas>1 => comparar TOTAL factura vs SUMA MONTO recibido
            # - NUEVO (ene 2026): si NO coincide con la referencia, antes de marcar ERROR probar
            #   si (MONTO recibido + ENVIO) coincide con el TOTAL. Si coincide, permitir facturar y avisar
            #   "CASO EXCEPCIONAL MONTO + ENVIO".
            try:
                tol = 0.50
                base_ok = bool(tabs_state.get(frm, True))  # no levantar una pestaña que ya era ERROR por stock/SKU

                use_monto_ref = bool(getattr(frm, "_has_cuotas_gt1", False))
                sum_neto = float(getattr(frm, "_sum_netos_all", 0.0) or 0.0)
                sum_monto = float(getattr(frm, "_sum_montos_all", 0.0) or 0.0)
                env_pago_total = float(getattr(frm, "_sum_envio_pago_raw", 0.0) or 0.0)

                sum_ref = sum_monto if use_monto_ref else sum_neto
                ok_ref = abs(sum_ref - total_items_sum) <= tol

                # Caso excepcional: MONTO + ENVIO
                ok_exc_monto_envio = abs((sum_monto + env_pago_total) - total_items_sum) <= tol if (env_pago_total > 0.0) else False

                if base_ok and (not ok_ref) and ok_exc_monto_envio:
                    # Permitir facturar, solo avisar.
                    frm._fact_excepcion_monto_envio = True
                    try:
                        tk.Label(
                            foot_frame,
                            text="CASO EXCEPCIONAL: MONTO + ENVIO",
                            font=("Segoe UI", 10, "bold"),
                            fg="#b45309",
                            bg="#ffffff",
                        ).pack(side="left")
                    except Exception:
                        pass
                    try:
                        nb.tab(frm, text=base_title)
                    except Exception:
                        pass
                    tabs_state[frm] = bool(tabs_state.get(frm, True))
                elif base_ok and (not ok_ref):
                    nb.tab(frm, text=f"ERROR — {base_title}")
                    tabs_state[frm] = False
                else:
                    tabs_state[frm] = bool(tabs_state.get(frm, True))
            except Exception:
                pass

            frm._fact_tab_key = k
            frm._fact_tab_title = base_title
            frm._fact_orders_group = orders_group
            frm._fact_vendor_cbo = cbo_vendor
            frm._fact_cost_entry = ent_cost
            frm._pagos_asociados = [{"pago_id": d.get("pago_id"), "orden": d.get("orden"), "neto": d.get("neto"), "pack": d.get("pack")}
                                    for d in pagos_all_visible]

        # ----------- Utilidades DB -----------
        def _db_path() -> Optional[str]:
            return getattr(owner, "_db_path", None)

        def _sum_cantidad_it_vent(remito: int) -> Optional[int]:
            dbp = _db_path()
            if not dbp or not os.path.isfile(dbp):
                return None
            try:
                con = sqlite3.connect(dbp)
                cur = con.cursor()
                cur.execute("PRAGMA table_info(it_vent)")
                cols = [r[1] for r in cur.fetchall()]
                remito_col = next((c for c in ["remito", "mr", "nroremito", "id_remito"] if c in cols), "remito")
                cant_col = next((c for c in ["cant", "cantidad"] if c in cols), None)
                if cant_col:
                    cur.execute(f"SELECT SUM(COALESCE({cant_col},0)) FROM it_vent WHERE {remito_col}=?", (int(remito),))
                    row = cur.fetchone()
                    val = int((row[0] or 0)) if row and row[0] is not None else 0
                else:
                    cur.execute(f"SELECT COUNT(*) FROM it_vent WHERE {remito_col}=?", (int(remito),))
                    row = cur.fetchone()
                    val = int((row[0] or 0)) if row and row[0] is not None else 0
                try:
                    con.close()
                except Exception:
                    pass
                return val
            except Exception:
                return None

        def _count_movimientos_mp(remito: int) -> Optional[int]:
            dbp = _db_path()
            if not dbp or not os.path.isfile(dbp):
                return None
            try:
                con = sqlite3.connect(dbp)
                cur = con.cursor()
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
                cur.execute("SELECT COUNT(*) FROM movimientos_mp WHERE remito_venta=?", (int(remito),))
                row = cur.fetchone()
                cnt = int(row[0] if row and row[0] is not None else 0)
                try:
                    con.close()
                except Exception:
                    pass
                return cnt
            except Exception:
                return None

        # ----------- Generación de ventas -----------
        def _check_success(remito: int, expected_items_count: int, pack_unique: str, orders_group: List[Dict[str, Any]], required_sku_qty: Optional[Dict[str, int]] = None, require_envio_6711: bool = False, compra_nc_envio_6711: Optional[int] = None) -> List[str]:
            details: List[str] = []
            details.append(f"  Pack ID: {pack_unique or '-'}")
            try:
                ords_txt = ", ".join(sorted(str(o.get("order_id")) for o in orders_group if o.get("order_id")))
            except Exception:
                ords_txt = ""
            details.append(f"  Órdenes: {ords_txt or '-'}")
            details.append(f"MR {remito}: Ítems esperados = {expected_items_count}")

            sum_qty = _sum_cantidad_it_vent(remito)
            if sum_qty is None:
                details.append("AVISO: No se pudo calcular SUM(it_vent.cantidad) (DB no válida).")
            else:
                if int(sum_qty) != int(expected_items_count):
                    details.append(f"ERROR: SUM(it_vent.cantidad) para MR {remito}: {sum_qty} (esperado {expected_items_count})")
                else:
                    details.append(f"OK: SUM(it_vent.cantidad) para MR {remito}: {sum_qty}")

            dbp = _db_path()
            if not dbp or not os.path.isfile(dbp):
                details.append("AVISO: No se pudo abrir DB para comprobaciones adicionales.")
                return details

            try:
                con = sqlite3.connect(dbp)
                cur = con.cursor()
                cur.execute("PRAGMA table_info(it_vent)")
                cols_i2 = [r[1] for r in cur.fetchall()]
                rem_i = next((c for c in ["remito", "mr", "nroremito", "id_remito"] if c in cols_i2), "remito")
                costo_col = next((c for c in ["costo", "cost", "precio_costo", "pcosto"] if c in cols_i2), None)
                art_col = next((c for c in ["articulo", "sku", "codigo", "art", "item"] if c in cols_i2), "articulo")
                # Chequeo por SKU para evitar falsos OK (ej: faltante 6711 MERCADO ENVIOS)
                if require_envio_6711 and not compra_nc_envio_6711:
                    details.append("ERROR: Se esperaba compra silenciosa de SKU 6711 (MERCADO ENVIOS) pero no se registró NC (compra).")
                if required_sku_qty:
                    try:
                        cur.execute(f"SELECT {art_col}, SUM(cantidad) FROM it_vent WHERE {rem_i}=? GROUP BY {art_col}", (int(remito),))
                        got = { _norm_sku(r[0]): int(r[1] or 0) for r in (cur.fetchall() or []) }
                        for sku_exp, qty_exp in (required_sku_qty or {}).items():
                            sku_n = _norm_sku(sku_exp)
                            if not sku_n:
                                continue
                            qty_got = int(got.get(sku_n, 0) or 0)
                            if int(qty_got) != int(qty_exp):
                                details.append(f"ERROR: it_vent MR {remito} — SKU {sku_n} cantidad {qty_got} (esperado {qty_exp})")
                    except Exception as ex_sku:
                        details.append(f"AVISO: chequeo por SKU MR {remito} falló: {ex_sku}")
                if costo_col:
                    cur.execute(f"SELECT COUNT(*) FROM it_vent WHERE {rem_i}=? AND ({costo_col} IS NULL OR {costo_col}=0)", (int(remito),))
                    zero_cost = int((cur.fetchone() or [0])[0] or 0)
                    details.append(("ERROR" if zero_cost > 0 else "OK") +
                                   (f": it_vent con costo=0 para MR {remito}: {zero_cost} registro(s)." if zero_cost > 0
                                    else f": it_vent sin costo=0 para MR {remito}."))
                else:
                    details.append("AVISO: it_vent sin columna de costo detectable; se omite chequeo costo=0.")
                cur.execute("PRAGMA table_info(codigos)")
                cols_c = [r[1] for r in cur.fetchall()]
                if "remito_ven" in cols_c:
                    cur.execute("SELECT COUNT(*) FROM codigos WHERE remito_ven=?", (int(remito),))
                    reserved = int((cur.fetchone() or [0])[0] or 0)
                    details.append(f"INFO: reservas en codigos MR {remito}: {reserved}")
                else:
                    details.append("AVISO: codigos sin columna remito_ven; se omite chequeo de reservas.")
                try:
                    con.close()
                except Exception:
                    pass
            except Exception as ex:
                details.append(f"AVISO: comprobaciones DB MR {remito} fallaron: {ex}")
            return details

        def _check_failure(expect_count: int, sku_qty_map: Dict[str, int], causa_error: str) -> List[str]:
            details: List[str] = [f"MR no generado: Ítems esperados = {expect_count}", f"Causa: {causa_error}"]
            dbp = _db_path()
            if not dbp or not os.path.isfile(dbp):
                details.append("AVISO: No se pudo abrir DB para comprobaciones (ruta no válida).")
                return details
            try:
                con = sqlite3.connect(dbp)
                cur = con.cursor()
                for sku, qty in sku_qty_map.items():
                    try:
                        cur.execute("""SELECT COUNT(*) FROM codigos
                                       WHERE articulo=? AND (deposito=1 OR TRIM(deposito)='1')
                                         AND (remito_ven IS NULL OR TRIM(CAST(remito_ven AS TEXT)) IN ('', '0'))""", (sku,))
                        libres = int((cur.fetchone() or [0])[0] or 0)
                        details.append(f"SKU {sku}: libres previos={libres}, pedido={qty}")
                    except Exception as exq:
                        details.append(f"AVISO: No se pudo consultar libres para SKU {sku}: {exq}")
                try:
                    con.close()
                except Exception:
                    pass
            except Exception as ex:
                details.append(f"AVISO: comprobaciones previas fallaron: {ex}")
            return details

        def _generar_todo():
            try:
                if sum(1 for v in tabs_state.values() if v) == 0:
                    messagebox.showwarning("Generar", "No hay pestañas OK para generar.", parent=win)
                    return

                missing = []
                for i in range(nb.index("end")):
                    frm_i = nb.nametowidget(nb.tabs()[i])
                    if tabs_state.get(frm_i) is False:
                        continue
                    ent_cost_i = getattr(frm_i, "_fact_cost_entry", None)
                    is_me_tab = bool(getattr(frm_i, "_is_me_group", False))
                    me_remove_6711 = bool(getattr(frm_i, "_wf_me_remove_6711", False))
                    # Nuestro costo de envío (traído de PREVIEW)
                    try:
                        nuestro_costo_envio_value = float(getattr(frm_i, "_nuestro_costo_envio_value", None) or getattr(frm_i, "_nuestro_costo_envio", 0.0) or 0.0)
                    except Exception:
                        try:
                            nuestro_costo_envio_value = float(_robust_parse_number(str(getattr(frm_i, "_nuestro_costo_envio", "0") or "0")))
                        except Exception:
                            nuestro_costo_envio_value = 0.0

                    # Si hay Entry debe estar seteado. Si NO hay Entry, sólo exigir cuando
                    # sea MERCADO ENVIOS y el detalle tenga un item ENVIO (no eliminado).
                    cost_ok = True
                    if ent_cost_i is not None:
                        cost_ok = bool(ent_cost_i.get().strip())
                    else:
                        if is_me_tab and (not bool(me_remove_6711)):
                            try:
                                cost_ok = float(nuestro_costo_envio_value or 0.0) > 0.0
                            except Exception:
                                cost_ok = False
                        else:
                            cost_ok = True
                    if not cost_ok:
                        k2 = getattr(frm_i, "_fact_tab_key", "")
                        missing.append(meta.get(k2, {}).get("id", k2) or f"Tab {i+1}")
                if missing:
                    messagebox.showwarning("Generar",
                                           f"No todas las facturas OK tienen Costo de Envío seteado: {', '.join(missing)}",
                                           parent=win)
                    return

                resultados: List[str] = []
                detalles_post: List[str] = []
                detalle_por_mr: Dict[int, List[str]] = {}
                errores_en_proceso = False
                sent_exc_emails = set()  # evita duplicados (por MR/ORD)

                def _send_exc_monto_envio_email(remito_num: int, order_id: str, sum_monto_v: float, env_pago_v: float, total_fact_v: float):
                    """Envía alerta automática cuando se detecta CASO EXCEPCIONAL: MONTO+ENVIO."""
                    try:
                        key = f"{remito_num}|{order_id}"
                        if key in sent_exc_emails:
                            return
                        sent_exc_emails.add(key)

                        # Buscar config SMTP
                        cfg_candidates = []
                        try:
                            cfg_candidates.append(os.path.join(os.path.dirname(__file__), 'email_ml_facturador.json'))
                        except Exception:
                            pass
                        cfg_candidates.extend([r'c:\\!GESTION2026\\email_ml_facturador.json', 'email_ml_facturador.json'])
                        cfg_path = next((cp for cp in cfg_candidates if cp and os.path.isfile(cp)), None)
                        if not cfg_path:
                            # No cortar el proceso si falta config
                            print('AVISO: no se encontró email_ml_facturador.json (no se envía alerta).')
                            return
                        with open(cfg_path, 'r', encoding='utf-8') as fcfg:
                            cfg = json.load(fcfg) if fcfg else {}

                        host = cfg.get('host') or cfg.get('smtp_host')
                        port = int(cfg.get('port') or cfg.get('smtp_port') or 587)
                        use_tls = bool(cfg.get('use_tls', True))
                        use_ssl = bool(cfg.get('use_ssl', False))
                        username = cfg.get('username') or cfg.get('user') or cfg.get('smtp_user')
                        password = cfg.get('password') or cfg.get('pass') or cfg.get('smtp_pass')
                        from_addr = cfg.get('from') or cfg.get('from_email') or username
                        to_list = cfg.get('to') or cfg.get('to_emails') or []
                        if isinstance(to_list, str):
                            to_list = [x.strip() for x in to_list.split(',') if x.strip()]
                        if not isinstance(to_list, list):
                            to_list = []
                        if not host or not from_addr or not to_list:
                            print(f"AVISO: config SMTP incompleta en {cfg_path} (host/from/to). No se envía alerta.")
                            return

                        total_combo = float(sum_monto_v or 0.0) + float(env_pago_v or 0.0)
                        diff = float(total_fact_v or 0.0) - total_combo

                        body = (
                            "Es posible que haya que revisar si el MercadoPago del envio NO SE MARCO en el listado.\n"
                            f"Remito: MR {remito_num}\n"
                            f"Venta: {order_id}\n\n"
                            f"MONTO: {self._fmt_money(sum_monto_v)}\n"
                            f"ENVIO: {self._fmt_money(env_pago_v)}\n"
                            f"MONTO+ENVIO: {self._fmt_money(total_combo)}\n"
                            f"TOTAL FACTURA: {self._fmt_money(total_fact_v)}\n"
                            f"DIF (Total - (Monto+Envio)): {self._fmt_money(diff)}\n"
                        )

                        msg = MIMEText(body, _charset='utf-8')
                        msg['Subject'] = 'CASO EXCEPCIONAL: MOTO+ENVIO'
                        msg['From'] = from_addr
                        msg['To'] = ', '.join(to_list)
                        msg['Date'] = formatdate(localtime=True)

                        if use_ssl:
                            server = smtplib.SMTP_SSL(host, port)
                        else:
                            server = smtplib.SMTP(host, port)
                        try:
                            server.ehlo()
                        except Exception:
                            pass
                        if use_tls and (not use_ssl):
                            try:
                                server.starttls()
                                server.ehlo()
                            except Exception:
                                pass
                        if username and password:
                            try:
                                server.login(username, password)
                            except Exception:
                                pass
                        server.sendmail(from_addr, to_list, msg.as_string())
                        try:
                            server.quit()
                        except Exception:
                            pass
                    except Exception as ex_email:
                        print(f"AVISO: no se pudo enviar email de CASO EXCEPCIONAL: {ex_email}")


                for i in range(nb.index("end")):
                    frm_i: tk.Frame = nb.nametowidget(nb.tabs()[i])
                    if tabs_state.get(frm_i, False) is False:
                        continue

                    base_title_i = getattr(frm_i, "_fact_tab_title", f"Tab {i+1}")
                    k2 = getattr(frm_i, "_fact_tab_key", "")
                    orders_group_i = getattr(frm_i, "_fact_orders_group", []) or []
                    cbo_vendor_i = getattr(frm_i, "_fact_vendor_cbo", None)
                    ent_cost_i = getattr(frm_i, "_fact_cost_entry", None)

                    # Ajustar shipping_cost_seller para MERCADO ENVIOS (por seguridad también acá)
                    try:
                        for o in orders_group_i:
                            if isinstance(o, dict) and (o.get("tipo_envio") or "").strip().upper() == "MERCADO ENVIOS":
                                corrected = _infer_envio_seller_for_order(o)
                                if corrected and corrected > 0:
                                    o["shipping_cost_seller"] = float(corrected)
                    except Exception:
                        pass

                    default_vendor = getattr(frm_i, "_default_vendor_name", "Pato")
                    vendor_name = (cbo_vendor_i.get().strip() if cbo_vendor_i else default_vendor)
                    vendor_name_up = vendor_name.upper()
                    envio_cost_text = (ent_cost_i.get().strip() if ent_cost_i else "")
                    is_me_tab = bool(getattr(frm_i, "_is_me_group", False))
                    me_remove_6711 = bool(getattr(frm_i, "_wf_me_remove_6711", False))
                    me_precio_6711 = float(getattr(frm_i, "_wf_me_precio_6711", 0.0) or 0.0)
                    me_sum_sin_envio = float(getattr(frm_i, "_wf_me_sum_sin_envio", 0.0) or 0.0)
                    # Nuestro costo de envío (traído de PREVIEW)
                    try:
                        nuestro_costo_envio_value = float(getattr(frm_i, "_nuestro_costo_envio_value", None) or getattr(frm_i, "_nuestro_costo_envio", 0.0) or 0.0)
                    except Exception:
                        try:
                            nuestro_costo_envio_value = float(_robust_parse_number(str(getattr(frm_i, "_nuestro_costo_envio", "0") or "0")))
                        except Exception:
                            nuestro_costo_envio_value = 0.0

                    # Para MERCADO ENVIOS, sólo usar costo autocalculado si hay un item "ENVIO" en el detalle.
                    if envio_cost_text:
                        envio_cost_value = _robust_parse_number(envio_cost_text)
                    elif is_me_tab and (not bool(me_remove_6711)):
                        envio_cost_value = float(nuestro_costo_envio_value or 0.0)
                    else:
                        envio_cost_value = 0.0

                    compra_nc_envio = None
                    codigo_envio_creado = None
                    compra_nc_error_flag = False
                    proveedor_error_flag = False

                    # Para facturas NO-ME: compra silenciosa 6696 si corresponde (moto)
                    if (not is_me_tab) and envio_cost_value > 0.0:
                        res_comp = ventas_ops.alta_compra_silenciosa_6696(vendor_name_up, envio_cost_value, deposito_codigos="1")
                        if res_comp.get("ok"):
                            compra_nc_envio = int(res_comp.get("compra"))
                            codigo_envio_creado = str(res_comp.get("codigo_envio") or "")
                            frm_i._compra_nc_envio = compra_nc_envio
                            frm_i._codigo_envio_creado = codigo_envio_creado

                            if compra_nc_envio > 100000:
                                compra_nc_error_flag = True
                                errores_en_proceso = True
                                resultados.append(f"{base_title_i}: ERROR->Numero de compra mal asignado (NC {compra_nc_envio})")
                                try:
                                    nb.tab(frm_i, text=f"ERROR — {base_title_i}")
                                except Exception:
                                    pass
                                tabs_state[frm_i] = False
                            else:
                                prov_code = res_comp.get("proveedor")
                                try:
                                    prov_code_int = int(prov_code)
                                except Exception:
                                    prov_code_int = 0
                                if prov_code_int == 0:
                                    proveedor_error_flag = True
                                    errores_en_proceso = True
                                    resultados.append(f"{base_title_i}: ERROR->Codigo de proveedor (prov 0)")
                                    try:
                                        nb.tab(frm_i, text=f"ERROR — {base_title_i}")
                                    except Exception:
                                        pass
                                    tabs_state[frm_i] = False
                                else:
                                    resultados.append(f"{base_title_i}: Compra 6696 al prov {prov_code_int} creada (NC {compra_nc_envio})")
                        else:
                            errores_en_proceso = True
                            resultados.append(f"{base_title_i}: FALLÓ compra 6696: {res_comp.get('error')}")

                    # NUEVO: Para MERCADO ENVIOS con Nuestro Costo > 0 y con item 'envio' en detalle,
                    # realizar PRE-COMPRA de 6711 (proveedor 034) antes de generar la venta.
                    compra_nc_envio_6711 = None
                    codigo_envio_creado_6711 = None
                    if is_me_tab and (not bool(me_remove_6711)) and float(nuestro_costo_envio_value or 0.0) > 0.0:
                        try:
                            if hasattr(ventas_ops, "alta_compra_silenciosa_6711_mercado_envios"):
                                res_comp_6711 = ventas_ops.alta_compra_silenciosa_6711_mercado_envios("034", float(nuestro_costo_envio_value or 0.0), deposito_codigos="1")
                            elif hasattr(ventas_ops, "alta_compra_silenciosa_sku"):
                                # Firma genérica esperada: (sku, proveedor, costo, deposito_codigos=...)
                                res_comp_6711 = ventas_ops.alta_compra_silenciosa_sku("6711", "034", float(nuestro_costo_envio_value or 0.0), deposito_codigos="1")
                            elif hasattr(ventas_ops, "alta_compra_silenciosa"):
                                # Firma genérica esperada: (sku, proveedor, costo, deposito_codigos=...)
                                res_comp_6711 = ventas_ops.alta_compra_silenciosa("6711", "034", float(nuestro_costo_envio_value or 0.0), deposito_codigos="1")
                            else:
                                raise AttributeError("ventas_ops no tiene método de compra silenciosa para SKU 6711")
                            if res_comp_6711.get("ok"):
                                compra_nc_envio_6711 = int(res_comp_6711.get("compra"))
                                codigo_envio_creado_6711 = str(res_comp_6711.get("codigo_envio") or "")
                                frm_i._compra_nc_envio_6711 = compra_nc_envio_6711
                                frm_i._codigo_envio_creado_6711 = codigo_envio_creado_6711
                                resultados.append(f"{base_title_i}: Compra 6711 MERCADO ENVIOS creada (NC {compra_nc_envio_6711})")
                            else:
                                errores_en_proceso = True
                                resultados.append(f"{base_title_i}: FALLÓ compra 6711 MERCADO ENVIOS: {res_comp_6711.get('error')}")
                                detalles_post.append("ERROR: Compra silenciosa 6711 (MERCADO ENVIOS) falló. Operación cancelada.")
                                try:
                                    nb.tab(frm_i, text=f"ERROR — {base_title_i}")
                                except Exception:
                                    pass
                                tabs_state[frm_i] = False
                                continue
                        except Exception as ex_pc:
                            errores_en_proceso = True
                            resultados.append(f"{base_title_i}: FALLÓ compra 6711 MERCADO ENVIOS: {ex_pc}")
                            detalles_post.append("ERROR: Compra silenciosa 6711 (MERCADO ENVIOS) falló (excepción). Operación cancelada.")
                            try:
                                nb.tab(frm_i, text=f"ERROR — {base_title_i}")
                            except Exception:
                                pass
                            tabs_state[frm_i] = False
                            continue

                    # NUEVO: Para MERCADO ENVIOS con bonificación ML (envío compartido),
                    # realizar PRE-COMPRA de 6756 (proveedor 034) antes de generar la venta.
                    compra_nc_envio_6756 = None
                    codigo_envio_creado_6756 = None
                    me_add_6756 = bool(getattr(frm_i, "_wf_me_add_6756", False))
                    me_bonificacion_ml = float(getattr(frm_i, "_wf_me_bonificacion_ml", 0.0) or 0.0)
                    if is_me_tab and me_add_6756 and me_bonificacion_ml > 0.0:
                        try:
                            if hasattr(ventas_ops, "alta_compra_silenciosa_6756_bonificacion_ml"):
                                res_comp_6756 = ventas_ops.alta_compra_silenciosa_6756_bonificacion_ml("034", float(me_bonificacion_ml), deposito_codigos="1")
                            elif hasattr(ventas_ops, "alta_compra_silenciosa_sku"):
                                # Firma genérica esperada: (sku, proveedor, costo, deposito_codigos=...)
                                # Para 6756, costo=0 pero amount es el precio de venta
                                res_comp_6756 = ventas_ops.alta_compra_silenciosa_sku("6756", "034", 0.0, deposito_codigos="1")
                            elif hasattr(ventas_ops, "alta_compra_silenciosa"):
                                # Firma genérica esperada: (sku, proveedor, costo, deposito_codigos=...)
                                res_comp_6756 = ventas_ops.alta_compra_silenciosa("6756", "034", 0.0, deposito_codigos="1")
                            else:
                                raise AttributeError("ventas_ops no tiene método de compra silenciosa para SKU 6756")
                            if res_comp_6756.get("ok"):
                                compra_nc_envio_6756 = int(res_comp_6756.get("compra"))
                                codigo_envio_creado_6756 = str(res_comp_6756.get("codigo_envio") or "")
                                frm_i._compra_nc_envio_6756 = compra_nc_envio_6756
                                frm_i._codigo_envio_creado_6756 = codigo_envio_creado_6756
                                resultados.append(f"{base_title_i}: Compra 6756 BONIFICACION ML creada (NC {compra_nc_envio_6756})")
                            else:
                                errores_en_proceso = True
                                resultados.append(f"{base_title_i}: FALLÓ compra 6756 BONIFICACION ML: {res_comp_6756.get('error')}")
                                detalles_post.append("ERROR: Compra silenciosa 6756 (BONIFICACION ML) falló. Operación cancelada.")
                                try:
                                    nb.tab(frm_i, text=f"ERROR — {base_title_i}")
                                except Exception:
                                    pass
                                tabs_state[frm_i] = False
                                continue
                        except Exception as ex_pc:
                            errores_en_proceso = True
                            resultados.append(f"{base_title_i}: FALLÓ compra 6756 BONIFICACION ML: {ex_pc}")
                            detalles_post.append("ERROR: Compra silenciosa 6756 (BONIFICACION ML) falló (excepción). Operación cancelada.")
                            try:
                                nb.tab(frm_i, text=f"ERROR — {base_title_i}")
                            except Exception:
                                pass
                            tabs_state[frm_i] = False
                            continue

                    if compra_nc_error_flag or proveedor_error_flag:
                        detalles_post.append("ERROR: Numero de compra mal asignado o Codigo de proveedor inválido (NC > 100000 o prov=0). Operación cancelada.")
                        continue

                    combined_items_i: List[Dict[str, Any]] = []
                    expected_items_count = 0
                    sku_qty_map: Dict[str, int] = {}

                    # BLINDAJE: si la pestaña ya tiene detalle normalizado (preview),
                    # usarlo como fuente única para GENERAR/validar (evita reglas duplicadas).
                    wf_items = getattr(frm_i, "_wf_combined_items", None)
                    if isinstance(wf_items, list) and wf_items:
                        for itx in wf_items:
                            try:
                                sku_b = _norm_sku(itx.get("sku") or itx.get("articulo") or itx.get("codigo") or "")
                            except Exception:
                                sku_b = ""
                            if not sku_b:
                                continue
                            nombre_b = itx.get("nombre") or itx.get("name") or ""
                            try:
                                qty_src = itx.get("cantidad", None)
                                if qty_src is None:
                                    qty_src = itx.get("quantity", 0)
                                qty_b = int(round(float(qty_src or 0)))
                            except Exception:
                                try:
                                    qty_b = int(itx.get("cantidad") or itx.get("quantity") or 0)
                                except Exception:
                                    qty_b = 0
                            try:
                                price_src = itx.get("precio", None)
                                if price_src is None:
                                    price_src = itx.get("price", 0.0)
                                price_b = float(price_src or 0.0)
                            except Exception:
                                price_b = 0.0
                            try:
                                subtotal_b = float(itx.get("subtotal") or itx.get("total") or (price_b * qty_b))
                            except Exception:
                                subtotal_b = float(price_b * qty_b)

                            row = {"sku": sku_b, "name": nombre_b, "quantity": qty_b, "price": price_b,
                                   "subtotal": subtotal_b, "total": subtotal_b}
                            try:
                                nce = float(itx.get("nuestro_costo_envio") or 0.0)
                                if nce > 0.0:
                                    row["nuestro_costo_envio"] = nce
                            except Exception:
                                pass
                            combined_items_i.append(row)
                            expected_items_count += max(0, int(qty_b or 0))
                            sku_qty_map[sku_b] = sku_qty_map.get(sku_b, 0) + max(0, int(qty_b or 0))
                        # Evita reconstrucción/duplicación desde orders_group_i (y evita aplicar reglas dos veces)
                        orders_group_i = []

                    for o in orders_group_i:
                        raw_items = (o.get("line_items") or o.get("productos") or [])
                        expanded_if_kit = _expand_kit_items_if_needed(owner, raw_items)
                        expanded_if_plus = _expand_plus_skus_if_needed(owner, expanded_if_kit)
                        for li in expanded_if_plus:
                            sku_raw = li.get("sku") or li.get("articulo") or li.get("codigo")
                            sku_base = _norm_sku(sku_raw)
                            try:
                                qty_base = int(li.get("quantity") or li.get("qty") or 0)
                            except Exception:
                                try:
                                    qty_base = int(float(str(li.get("quantity") or "0")))
                                except Exception:
                                    qty_base = 0
                            nombre_original = str(li.get("name") or li.get("title") or "")
                            nombre = nombre_original or _get_articulo_descrip(owner, sku_base)

                            subtotal = li.get("total") if li.get("total") is not None else (li.get("subtotal") if li.get("subtotal") is not None else None)
                            if subtotal is None:
                                try:
                                    precio = float(li.get("price") or li.get("unit_price") or 0.0)
                                    subtotal = precio * qty_base
                                except Exception:
                                    precio = 0.0
                                    subtotal = 0.0
                            try:
                                precio = float(li.get("price") or li.get("unit_price") or (float(subtotal)/qty_base if qty_base else 0.0))
                            except Exception:
                                precio = 0.0

                            # Aplicar multiplicadores con excepciones:
                            sku_base2, qty_after_sku_mult, price_after_sku_mult = _apply_sku_multiplier_if_present(str(sku_raw or ""), qty_base, precio)
                            sku_norm2 = _norm_sku(sku_base2)
                            only_sku_x = (sku_norm2 == _norm_sku("5293"))
                            skip_all_mult = _sku_skip_rule(sku_norm2, "MULT")

                            pack_mult_name = _pack_multiplier_from_name(nombre)
                            
                            # Descripción del ítem (si está disponible). Si no viene, se cae al título.
                            desc_txt = str(li.get("description") or li.get("descripcion") or li.get("desc") or "")
                            
                            # Multiplicador por descripción: usa la descripción real, no el título
                            pack_mult_desc = _pack_multiplier_from_description(desc_txt or nombre)
                            
                            # EXCEPCION: SKU 6404 => NO aplicar multiplicador 'PACK XN' desde descripcion/titulo
                            if sku_norm2 == _norm_sku("6404"):
                                pack_mult_desc = 1.0
                            if sku_norm2 == _norm_sku("6405"):
                                pack_mult_desc = 1.0
                            # Excepción pedida: para 1637/1640, si TÍTULO o DESCRIPCIÓN tiene una 'X' en las primeras letras,
                            # no aplicar multiplicadores automáticos por nombre/desc/metros.
                            # Excepción pedida: para 1630/1637/1640, si TÍTULO o DESCRIPCIÓN tiene una 'X' en las primeras letras

                            # (ej: '10 X ...' o 'X 5 ...'), no aplicar multiplicadores automáticos por nombre/desc/metros.

                            x_first = (_has_x_in_first_letters(nombre) or _has_x_in_first_letters(desc_txt))

                            if sku_norm2 in {_norm_sku("1637"),_norm_sku("1640"),_norm_sku("1644"), _norm_sku("1341"), _norm_sku("1647")} and x_first:

                                pack_mult_name = 1.0

                                pack_mult_desc = 1.0
                            if _sku_skip_rule(sku_norm2, "PACK") or skip_all_mult or only_sku_x:
                                pack_mult_name = 1.0
                                pack_mult_desc = 1.0
                            pack_mult = pack_mult_name * pack_mult_desc
                            metros_mult = 1.0 if (sku_norm2 == _norm_sku("5283") or skip_all_mult or only_sku_x or (sku_norm2 in {_norm_sku("1637"),_norm_sku("1640"),_norm_sku("1644"), _norm_sku("1341"), _norm_sku("1647")} and x_first)) else _metros_multiplier_from_name(nombre)

                            qty = int(round(qty_after_sku_mult * pack_mult * metros_mult))
                            precio_ajustado = price_after_sku_mult / ((pack_mult * metros_mult) if (pack_mult * metros_mult) > 0 else 1.0)

                            combined_items_i.append({
                                "sku": sku_norm2, "name": nombre,
                                "quantity": qty, "price": precio_ajustado,
                                "subtotal": float(precio_ajustado * qty), "total": float(precio_ajustado * qty),
                            })
                            expected_items_count += int(qty or 0)
                            sku_qty_map[sku_norm2] = sku_qty_map.get(sku_norm2, 0) + int(qty or 0)

                    envio_visual_value = float(getattr(frm_i, "_envio_visual_amount", 0.0) or 0.0)

                    # Blindaje: 6696 (envío FLEX) no puede aparecer 2 veces en la venta.
                    sku_6696 = _norm_sku("6696")
                    try:
                        existing_6696 = [li for li in combined_items_i if _norm_sku(li.get("sku")) == sku_6696]
                    except Exception:
                        existing_6696 = []

                    if envio_visual_value > 0.0 and (not is_me_tab):
                        if existing_6696:
                            removed_qty = 0
                            try:
                                removed_qty = sum(int(li.get("quantity") or 0) for li in existing_6696)
                            except Exception:
                                for li in existing_6696:
                                    try:
                                        removed_qty += int(li.get("quantity") or 0)
                                    except Exception:
                                        pass
                            # sacar 6696 existentes
                            combined_items_i = [li for li in combined_items_i if _norm_sku(li.get("sku")) != sku_6696]
                            # ajustar contadores
                            try:
                                expected_items_count = max(0, int(expected_items_count) - int(removed_qty))
                            except Exception:
                                pass
                            try:
                                sku_qty_map[sku_6696] = int(sku_qty_map.get(sku_6696, 0) or 0) - int(removed_qty)
                                if sku_qty_map.get(sku_6696, 0) <= 0:
                                    sku_qty_map.pop(sku_6696, None)
                            except Exception:
                                sku_qty_map.pop(sku_6696, None)

                        combined_items_i.append({
                            "sku": sku_6696, "name": "Envio por moto flex", "quantity": 1,
                            "price": float(envio_visual_value), "subtotal": float(envio_visual_value), "total": float(envio_visual_value),
                        })
                        expected_items_count += 1
                        sku_qty_map[sku_6696] = sku_qty_map.get(sku_6696, 0) + 1
                    else:
                        # Si no vamos a inyectar 6696 pero vinieron duplicados, colapsarlos a uno.
                        if len(existing_6696) > 1:
                            removed_qty = 0
                            total_sum = 0.0
                            for li in existing_6696:
                                try:
                                    removed_qty += int(li.get("quantity") or 0)
                                except Exception:
                                    pass
                                try:
                                    total_sum += float(li.get("total") or li.get("subtotal") or 0.0)
                                except Exception:
                                    pass
                            combined_items_i = [li for li in combined_items_i if _norm_sku(li.get("sku")) != sku_6696]
                            try:
                                expected_items_count = max(0, int(expected_items_count) - int(removed_qty))
                            except Exception:
                                pass
                            try:
                                sku_qty_map[sku_6696] = int(sku_qty_map.get(sku_6696, 0) or 0) - int(removed_qty)
                                if sku_qty_map.get(sku_6696, 0) <= 0:
                                    sku_qty_map.pop(sku_6696, None)
                            except Exception:
                                sku_qty_map.pop(sku_6696, None)
                            combined_items_i.append({
                                "sku": sku_6696, "name": "Envio por moto flex", "quantity": 1,
                                "price": float(total_sum), "subtotal": float(total_sum), "total": float(total_sum),
                            })
                            expected_items_count += 1
                            sku_qty_map[sku_6696] = sku_qty_map.get(sku_6696, 0) + 1
                    # MERCADO ENVIOS: normalización de ítems (quitar 6696 / ajustar 6711) se aplica más abajo.

                    total_venta = float(sum((it.get("subtotal") or it.get("total") or 0.0) for it in combined_items_i))
                    detalle_tmp: List[str] = []

                    # MERCADO ENVIOS — Normalización de ítems de envío
                    if is_me_tab and float(nuestro_costo_envio_value or 0.0) > 0.0:
                        costo_me = float(nuestro_costo_envio_value or 0.0)

                        # Si no vinieron flags del UI, recomputar suma sin envío
                        try:
                            if float(me_sum_sin_envio or 0.0) <= 0.0:
                                me_sum_sin_envio = float(sum((it.get("subtotal") or it.get("total") or 0.0) for it in (combined_items_i or [])
                                                             if _norm_sku(it.get("sku")) not in (_norm_sku("6696"), _norm_sku("6711"), _norm_sku("6756"))))
                        except Exception:
                            pass
                        try:
                            neto_ref_i = float(getattr(frm_i, "_sum_netos_all", 0.0) or 0.0)
                        except Exception:
                            neto_ref_i = 0.0

                        # Determinar si se elimina 6711 (caso <33k y neto==sin_envio)
                        if float(me_sum_sin_envio or 0.0) < 33000.0 and abs(float(me_sum_sin_envio or 0.0) - float(neto_ref_i or 0.0)) <= 0.50:
                            me_remove_6711 = True
                        else:
                            me_remove_6711 = bool(me_remove_6711)

                        # Precio del 6711: <33k => igual al costo; >=33k => 0
                        me_precio_6711 = float(costo_me) if (float(me_sum_sin_envio or 0.0) < 33000.0) else 0.0

                        # Persistir flags para validaciones/checks
                        frm_i._wf_me_remove_6711 = bool(me_remove_6711)
                        frm_i._wf_me_precio_6711 = float(me_precio_6711 or 0.0)
                        frm_i._wf_me_sum_sin_envio = float(me_sum_sin_envio or 0.0)

                        # Filtrar cualquier 6696/6711/6756 preexistente del detalle
                        removed_6696 = 0
                        removed_6711 = 0
                        removed_6756 = 0
                        _new_items = []
                        for _it in (combined_items_i or []):
                            _sku = _norm_sku(_it.get("sku"))
                            if _sku == _norm_sku("6696"):
                                try:
                                    removed_6696 += int(_it.get("quantity") or 0)
                                except Exception:
                                    removed_6696 += 0
                                continue
                            if _sku == _norm_sku("6711"):
                                try:
                                    removed_6711 += int(_it.get("quantity") or 0)
                                except Exception:
                                    removed_6711 += 0
                                continue
                            if _sku == _norm_sku("6756"):
                                try:
                                    removed_6756 += int(_it.get("quantity") or 0)
                                except Exception:
                                    removed_6756 += 0
                                continue
                            _new_items.append(_it)

                        combined_items_i = _new_items

                        # Ajustar conteos esperados / mapa SKUs por lo removido
                        if removed_6696:
                            try:
                                expected_items_count -= int(removed_6696)
                            except Exception:
                                pass
                            sku_qty_map.pop(_norm_sku("6696"), None)
                        if removed_6711:
                            try:
                                expected_items_count -= int(removed_6711)
                            except Exception:
                                pass
                            sku_qty_map.pop(_norm_sku("6711"), None)
                        if removed_6756:
                            try:
                                expected_items_count -= int(removed_6756)
                            except Exception:
                                pass
                            sku_qty_map.pop(_norm_sku("6756"), None)

                        # Reinsertar 6711 si corresponde
                        if not bool(me_remove_6711):
                            combined_items_i.append({
                                "sku": _norm_sku("6711"),
                                "name": "Envio por Mercado Envios",
                                "quantity": 1,
                                "stock": 1,
                                "nuestro_costo_envio": float(costo_me),
                                "price": float(me_precio_6711),
                                "precio": float(me_precio_6711),
                                "subtotal": float(me_precio_6711),
                                "total": float(me_precio_6711),
                            })
                            try:
                                expected_items_count += 1
                            except Exception:
                                pass
                            try:
                                sku_qty_map[_norm_sku("6711")] = 1
                            except Exception:
                                pass

                        # NUEVO: Reinsertar 6756 si hay bonificación ML (envío compartido)
                        me_add_6756 = bool(getattr(frm_i, "_wf_me_add_6756", False))
                        me_bonificacion_ml = float(getattr(frm_i, "_wf_me_bonificacion_ml", 0.0) or 0.0)
                        if me_add_6756 and me_bonificacion_ml > 0.0:
                            combined_items_i.append({
                                "sku": _norm_sku("6756"),
                                "name": "Envio Bonificacion de MercadoLibre",
                                "quantity": 1,
                                "stock": 1,
                                "nuestro_costo_envio": 0.0,
                                "costo": 0.0,
                                "price": float(me_bonificacion_ml),
                                "precio": float(me_bonificacion_ml),
                                "subtotal": float(me_bonificacion_ml),
                                "total": float(me_bonificacion_ml),
                            })
                            try:
                                expected_items_count += 1
                            except Exception:
                                pass
                            try:
                                sku_qty_map[_norm_sku("6756")] = 1
                            except Exception:
                                pass

                        total_venta = float(sum((it.get("subtotal") or it.get("total") or 0.0) for it in combined_items_i))

                    try:
                        pack_unique = str(next((o.get("pack_id") for o in orders_group_i if o.get("pack_id")), "")) or ""
                    except Exception:
                        pack_unique = ""
                    try:
                        ords_txt = ", ".join(sorted(str(o.get("order_id")) for o in orders_group_i if o.get("order_id")))
                    except Exception:
                        ords_txt = ""

                    detalle_tmp.append(f"  Pack ID: {pack_unique or '-'}")
                    detalle_tmp.append(f"  Órdenes: {ords_txt or '-'}")
                    for it in combined_items_i:
                        q = int(it.get("quantity") or 0)
                        unit = (float(it.get("subtotal") or 0.0) / q) if q else float(it.get("price") or 0.0)
                        detalle_tmp.append(f"  - {it.get('sku')} x{q} @ {self._fmt_money(unit)} = {self._fmt_money(float(it.get('subtotal') or 0.0))}")
                    detalle_tmp.append(f"  Total (según detalle): {self._fmt_money(total_venta)}")

                    total_neto_esperado = float(getattr(frm_i, "_sum_netos_all", 0.0))
                    total_monto_esperado = float(getattr(frm_i, "_sum_montos_all", 0.0))
                    try:
                        tol = 0.50
                        use_monto_ref = bool(getattr(frm_i, "_has_cuotas_gt1", False))
                        ref_val = float(total_monto_esperado if use_monto_ref else total_neto_esperado)
                        ok_ref = abs(total_venta - ref_val) <= tol
                        # Excepción: si hay cuotas>1, el chequeo se hace contra MONTO (no contra NETO)
                        estado_linea = "OK" if ok_ref else "ERROR"
                        detalle_tmp.append(
                            f"  Esperados: Neto={self._fmt_money(total_neto_esperado)} / Monto={self._fmt_money(total_monto_esperado)} -> {estado_linea}"
                        )

                        # Extra: si no coincide con NETO/MONTO de referencia, mostrar el OK excepcional por MONTO + ENVIO (si aplica)
                        try:
                            env_pago_total = float(getattr(frm_i, "_sum_envio_pago_raw", 0.0) or 0.0)
                        except Exception:
                            env_pago_total = 0.0
                        ok_exc_monto_envio = (env_pago_total > 0.0) and (abs((total_monto_esperado + env_pago_total) - total_venta) <= tol)
                        if (not ok_ref) and ok_exc_monto_envio:
                            detalle_tmp.append(
                                f"  OK (CASO EXCEPCIONAL MONTO + ENVIO): "
                                f"Monto {self._fmt_money(total_monto_esperado)} + Envío {self._fmt_money(env_pago_total)} = {self._fmt_money(total_monto_esperado + env_pago_total)} "
                                f"vs Total {self._fmt_money(total_venta)} (Dif {self._fmt_money((total_monto_esperado + env_pago_total) - total_venta)})"
                            )
                    except Exception:
                        detalle_tmp.append(f"  Esperados: Neto={self._fmt_money(total_neto_esperado)}")

                    cli_disp = ""
                    if orders_group_i:
                        o0 = orders_group_i[0]
                        cli_code = str(o0.get("buyer_code") or o0.get("cliente_codigo") or "").strip()
                        cli_name = str(o0.get("buyer_nickname") or o0.get("buyer") or "").strip()
                        cli_disp = f"{cli_code} — {cli_name}" if (cli_code and cli_name) else (cli_name or cli_code or "no se sabe")

                    es_flex = (vendor_name_up in {"PATO", "JHONATAN"}) or (envio_visual_value > 0.0 and any(it.get("sku") == _norm_sku("6696") for it in combined_items_i))
                    tipo_envio_pedido = ""
                    try:
                        if orders_group_i and isinstance(orders_group_i[0], dict):
                            tipo_envio_pedido = str(orders_group_i[0].get("tipo_envio") or "").strip()
                    except Exception:
                        tipo_envio_pedido = ""
                    if (not tipo_envio_pedido) and is_me_tab:
                        tipo_envio_pedido = "MERCADO ENVIOS"

                    pedido = {
                        "order_id": meta.get(k2, {}).get("id") or (orders_group_i[0].get("order_id") if orders_group_i else base_title_i),
                        "cliente_edit": cli_disp,
                        "pago_edit": "TRANSFERENCIA",
                        "line_items": combined_items_i,
                        "total": total_venta,
                        "tipo_envio": tipo_envio_pedido,
                        "is_me_tab": bool(is_me_tab),
                    }
                    pagos_asoc = list(getattr(frm_i, "_pagos_asociados", []) or [])
                    usuario_ml = _resolve_usuario_ml(owner)
                    pagos_esperados = len(pagos_asoc)

                    # Pasar info de compra silenciosa 6696 (si existe) y 6711 (si existe) a la venta
                    compra_nc_for_envio_param = (getattr(frm_i, "_compra_nc_envio", None) or getattr(frm_i, "_compra_nc_envio_6711", None))
                    codigo_envio_creado_param = (getattr(frm_i, "_codigo_envio_creado", None) or getattr(frm_i, "_codigo_envio_creado_6711", None))

                    res_ven = ventas_ops.alta_venta_silenciosa_directa(
                        pedido,
                        vendor_name=vendor_name,
                        envio_cost_value=envio_cost_value,
                        es_flex=es_flex,
                        compra_nc_for_envio=(compra_nc_for_envio_param),
                        codigo_envio_creado=(codigo_envio_creado_param or None),
                        pagos_asociados=pagos_asoc,
                        usuario_ml=usuario_ml,
                        envio_cobro_value=envio_visual_value
                    )

                    if res_ven.get("ok"):
                        remito = int(res_ven.get("remito"))
                        resultados.append(f"{base_title_i}: Remito de venta generado MR {remito}")
                        try:
                            if bool(getattr(frm_i, "_fact_excepcion_monto_envio", False)):
                                order_id_mail = str(pedido.get("order_id") or (meta.get(k2, {}) or {}).get("id") or "").strip()
                                try:
                                    env_pago_mail = float(getattr(frm_i, "_sum_envio_pago_raw", 0.0) or 0.0)
                                except Exception:
                                    env_pago_mail = 0.0
                                _send_exc_monto_envio_email(remito, order_id_mail, float(total_monto_esperado), float(env_pago_mail), float(total_venta))
                        except Exception:
                            pass


                        pagos_registrados = _count_movimientos_mp(remito)
                        if pagos_registrados is None:
                            detalle_tmp.append(f"  Pagos Esperados: {pagos_esperados} — Pagos registrados: (no disponible)")
                        else:
                            status = "OK" if int(pagos_registrados) == int(pagos_esperados) else "ERROR"
                            detalle_tmp.append(f"  Pagos Esperados: {pagos_esperados} — Pagos registrados: {int(pagos_registrados)} -> {status}")

                        detalles_post.extend(_check_success(remito, expected_items_count, pack_unique, orders_group_i,
                                                     required_sku_qty=sku_qty_map,
                                                     require_envio_6711=(is_me_tab and (not bool(me_remove_6711)) and float(nuestro_costo_envio_value or 0.0) > 0.0),
                                                     compra_nc_envio_6711=compra_nc_envio_6711))
                        detalle_por_mr[remito] = detalle_tmp
                    else:
                        errores_en_proceso = True
                        resultados.append(f"{base_title_i}: FALLÓ alta de venta: {res_ven.get('error')}")
                        detalle_tmp.append(f"  Pagos Esperados: {pagos_esperados}")
                        detalles_post.extend(_check_failure(expected_items_count, sku_qty_map, str(res_ven.get('error'))))

                hay_error = (
                    errores_en_proceso
                    or any(line.startswith("ERROR") or "ERROR:" in line for line in detalles_post)
                    or any(line.startswith("FALLÓ") for line in resultados)
                )

                out_win = tk.Toplevel(win)
                out_win.title("Resultados de Generación")
                try:
                    out_win.geometry("1200x740+100+80")
                except Exception:
                    pass
                frm_res = tk.Frame(out_win, bg="#ffffff")
                frm_res.pack(fill="both", expand=True, padx=12, pady=12)

                lbl_status = tk.Label(frm_res, text=("ERROR" if hay_error else "OK"),
                                      font=("Segoe UI", 18, "bold"),
                                      fg=("#b91c1c" if hay_error else "#16a34a"),
                                      bg="#ffffff")
                lbl_status.pack(anchor="w", pady=(0, 8))

                tk.Label(frm_res, text="Resumen:", font=("Segoe UI", 11, "bold"), bg="#ffffff").pack(anchor="w", pady=(0, 8))
                txt = tk.Text(frm_res, height=28, bg="#fafafa")
                txt.pack(fill="both", expand=True)
                for line in resultados:
                    txt.insert("end", f"{line}\n")
                if detalle_por_mr:
                    txt.insert("end", "\nDetalles por MR:\n")
                    for mr, lines in detalle_por_mr.items():
                        txt.insert("end", f"- MR {mr}:\n")
                        for ln in lines:
                            txt.insert("end", f"{ln}\n")
                txt.insert("end", "\nComprobaciones:\n")
                for line in detalles_post:
                    txt.insert("end", f"- {line}\n")
                txt.configure(state="disabled")

                def _send_email_with_report():
                    try:
                        cfg_path = r"c:\!GESTION2026\email.json"
                        if not os.path.isfile(cfg_path):
                            messagebox.showerror("Email", f"No se encontró la configuración SMTP: {cfg_path}", parent=out_win)
                            return
                        with open(cfg_path, "r", encoding="utf-8") as f:
                            cfg = json.load(f)

                        txt.configure(state="normal")
                        body = txt.get("1.0", "end")
                        txt.configure(state="disabled")

                        msg = MIMEText(body, _charset="utf-8")
                        msg["Subject"] = "Error en ML FACTURATOR" if hay_error else "ML FACTURATOR — Resultado"
                        msg["From"] = cfg.get("from") or cfg.get("username")
                        to_list = cfg.get("to") or ["miguel.pulignano@gmail.com"]
                        msg["To"] = ", ".join(to_list)
                        msg["Date"] = formatdate(localtime=True)

                        host = cfg.get("host")
                        port = int(cfg.get("port") or 587)
                        use_tls = bool(cfg.get("use_tls"))
                        use_ssl = bool(cfg.get("use_ssl"))
                        username = cfg.get("username")
                        password = cfg.get("password")

                        if use_ssl:
                            server = smtplib.SMTP_SSL(host, port)
                        else:
                            server = smtplib.SMTP(host, port)

                        server.ehlo()
                        if use_tls and not use_ssl:
                            try:
                                server.starttls()
                                server.ehlo()
                            except Exception:
                                pass

                        if username and password:
                            server.login(username, password)

                        server.sendmail(msg["From"], to_list, msg.as_string())
                        try:
                            server.quit()
                        except Exception:
                            pass
                        messagebox.showinfo("Email", "Reporte enviado por email.", parent=out_win)
                    except Exception as e:
                        messagebox.showerror("Email", f"No se pudo enviar el email:\n{e}")

                footer2 = tk.Frame(out_win, bg="#ffffff")
                footer2.pack(fill="x", pady=(8, 4))
                tk.Button(footer2, text="EMAIL", command=_send_email_with_report,
                          bg="#2563eb", fg="#fff",
                          font=("Segoe UI", 10, "bold"), padx=10, pady=4).pack(side="right")

                def _close_module_completely():
                    try:
                        out_win.destroy()
                    except Exception:
                        pass
                    try:
                        win.destroy()
                    except Exception:
                        pass
                    try:
                        root = getattr(owner, "root", None)
                        if root:
                            root.quit()
                    except Exception:
                        pass
                    try:
                        os._exit(0)
                    except Exception:
                        import sys
                        sys.exit(0)

                tk.Button(footer2, text="CERRAR", command=_close_module_completely,
                          bg="#000000", fg="#ffffff",
                          font=("Segoe UI", 10, "bold"), padx=10, pady=4).pack(side="right", padx=8)

            except Exception as e:
                messagebox.showerror("Generar", f"Error al generar facturas:\n{e}", parent=win)

        def _close():
            try:
                win.destroy()
            except Exception:
                try:
                    win.quit()
                except Exception:
                    pass

        footer = tk.Frame(win, bg="#f3f4f6")
        footer.pack(fill="x", pady=(6, 10))
        btn_generar = tk.Button(footer, text="GENERAR TODO", command=_generar_todo, bg="#f08a00", fg="#fff",
                                font=("Segoe UI", 10, "bold"), padx=10, pady=4)
        btn_generar.pack(side="right", padx=8)
        tk.Button(footer, text="CERRAR", command=_close, bg="#990000", fg="#fff",
                  font=("Segoe UI", 10, "bold"), padx=10, pady=4).pack(side="right", padx=8)

        _refresh_ok_err_and_button()

        try:
            win.transient(None)
        except Exception:
            pass
        win.focus_set()

    # --------------------- Utilidades comunes ---------------------

    def _stock_map_for_skus(self, skus: List[str]) -> Dict[str, int]:
        norm_skus = [_norm_sku(s) for s in skus if s]
        out = {s: 0 for s in norm_skus}
        if not out:
            return {}
        db = getattr(self, "_db_path", None)
        if not db or not os.path.exists(db):
            if _norm_sku("0888") in out:
                out[_norm_sku("0888")] = 1
            if _norm_sku("6696") in out:
                out[_norm_sku("6696")] = 1
            return out
        try:
            con = sqlite3.connect(db)
            cur = con.cursor()
            for s in list(out.keys()):
                try:
                    cur.execute(
                        "SELECT COUNT(*) FROM codigos "
                        "WHERE TRIM(articulo)=? "
                        "AND (deposito=1 OR TRIM(deposito)='1') "
                        "AND (remito_ven IS NULL OR TRIM(CAST(remito_ven AS TEXT)) IN ('', '0'))",
                        (s.strip(),)
                    )
                    row = cur.fetchone()
                    out[s] = int(row[0] if row and row[0] is not None else 0)
                except Exception:
                    out[s] = 0
            try:
                con.close()
            except Exception:
                pass
            if _norm_sku("0888") in out:
                out[_norm_sku("0888")] = max(1, int(out.get(_norm_sku("0888")) or 0))
            if _norm_sku("6696") in out:
                out[_norm_sku("6696")] = max(1, int(out.get(_norm_sku("6696")) or 0))
        except Exception:
            if _norm_sku("0888") in out:
                out[_norm_sku("0888")] = 1
            if _norm_sku("6696") in out:
                out[_norm_sku("6696")] = 1
        return out

    def _fmt_money(self, v: Any) -> str:
        try:
            f = float(v or 0.0)
            return f"{f:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except Exception:
            return str(v)

    def _parse_fecha_ml(self, s: Optional[str]) -> datetime:
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except Exception:
            try:
                return datetime.now(timezone.utc)
            except Exception:
                return datetime.utcfromtimestamp(0).replace(tzinfo=timezone.utc)

    def _fmt_dt_ar(self, dt_val: Optional[datetime]) -> str:
        try:
            if not dt_val:
                return ""
            return (dt_val.astimezone(AR_TZ)).strftime("%d/%m/%Y %H:%M:%S")
        except Exception:
            return ""