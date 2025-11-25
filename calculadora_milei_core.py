
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Tuple


@dataclass
class Bracket:
    minimo: float
    maximo: float  # usar float("inf") para sin tope superior
    fijo: float


@dataclass
class DesgloseVenta:
    precio_venta: float
    costo_total: float
    comision_variable: float
    comision_fija: float
    limpio: float
    ganancia_neta: float


# NUEVOS COSTOS FIJOS (reemplazan a los anteriores)
# En productos hasta $ 15.000, pagás $ 1.115 por unidad vendida.
# Entre $ 15.000 y $ 25.000, pagás $ 2.300 por unidad vendida.
# Entre $ 25.000 y $ 33.000, pagás $ 2.810 por unidad vendida.
# Suposición: a partir de $ 33.000 no hay costo fijo.
BRACKETS_SUELTOS: List[Bracket] = [
    Bracket(minimo=0, maximo=15000, fijo=1115),
    Bracket(minimo=15000, maximo=25000, fijo=2300),
    Bracket(minimo=25000, maximo=33000, fijo=2810),
    Bracket(minimo=33000, maximo=float("inf"), fijo=0),
]

# Para packs usamos la misma tabla de costos fijos
BRACKETS_PACK10: List[Bracket] = list(BRACKETS_SUELTOS)


def _buscar_bracket(precio_venta: float, brackets: Iterable[Bracket]) -> Bracket:
    for br in brackets:
        if br.minimo <= precio_venta < br.maximo:
            return br
    # fallback: último
    return list(brackets)[-1]


def desglose_venta(
    precio_venta: float,
    costo_total: float,
    comision_ml: float,
    brackets: Iterable[Bracket],
) -> DesgloseVenta:
    """
    Devuelve el desglose de una venta:

    - precio_venta: precio final cobrado al cliente
    - costo_total: costo de la mercadería
    - comision_ml: porcentaje de comisión variable (ej: 0.1435 = 14,35%)
    - brackets: tabla de costos fijos según rango de precio
    """
    br = _buscar_bracket(precio_venta, brackets)
    comision_variable = precio_venta * comision_ml
    comision_fija = br.fijo
    limpio = precio_venta - comision_variable - comision_fija
    ganancia_neta = limpio - costo_total
    return DesgloseVenta(
        precio_venta=precio_venta,
        costo_total=costo_total,
        comision_variable=comision_variable,
        comision_fija=comision_fija,
        limpio=limpio,
        ganancia_neta=ganancia_neta,
    )


def _calc_precio_objetivo(
    costo_total: float,
    ganancia_factor: float,
    comision_ml: float,
    brackets: Iterable[Bracket],
) -> float:
    """
    Calcula el precio que deberíamos publicar para que la ganancia neta sea:

        ganancia_neta = costo_total * ganancia_factor

    considerando comisión variable + fija.

    Resolvemos la ecuación:
        precio * (1 - comision_ml) - fijo - costo_total = costo_total * ganancia_factor
    """
    # Como el costo fijo depende del precio, iteramos unas veces hasta converger.
    precio_est = costo_total * (1 + ganancia_factor) / max(0.01, (1 - comision_ml))
    for _ in range(6):
        br = _buscar_bracket(precio_est, brackets)
        numerador = costo_total * (1 + ganancia_factor) + br.fijo
        denominador = 1 - comision_ml
        if denominador <= 0:
            denominador = 0.01
        nuevo = numerador / denominador
        if abs(nuevo - precio_est) < 1e-3:
            break
        precio_est = nuevo
    return max(0.0, precio_est)


def tabla_sueltos(
    costo_unitario: float,
    ganancia_factor: float,
    comision_ml: float,
    cantidades: Iterable[int],
):
    """
    Devuelve una lista de filas para la pestaña de ARTÍCULOS SUELTOS.

    Cada fila es un dict con:
        - cantidad
        - costo_total
        - precio_ml  (float, sin redondear)
    """
    filas = []
    for cant in cantidades:
        if cant <= 0:
            continue
        costo_total = costo_unitario * cant
        precio_ml = _calc_precio_objetivo(
            costo_total=costo_total,
            ganancia_factor=ganancia_factor,
            comision_ml=comision_ml,
            brackets=BRACKETS_SUELTOS,
        )
        filas.append(
            {
                "cantidad": cant,
                "costo_total": costo_total,
                "precio_ml": precio_ml,
            }
        )
    return filas


def tabla_pack_x10(
    costo_pack_10: float,
    ganancia_factor: float,
    comision_ml: float,
    unidades_en_pack: Iterable[int],
):
    """
    Devuelve una lista de filas para la pestaña PACK x 10.

    - costo_pack_10: costo de un pack de 10 unidades
    - ganancia_factor: factor de ganancia sobre el costo total
    - comision_ml: porcentaje de comisión variable
    - unidades_en_pack: cantidades finales (ej: 2,3,4,5,10,50,...)

    Se parte de un costo unitario = costo_pack_10 / 10.
    """
    if costo_pack_10 < 0:
        costo_pack_10 = 0.0
    costo_unitario = costo_pack_10 / 10.0 if costo_pack_10 else 0.0

    filas = []
    for unidades in unidades_en_pack:
        if unidades <= 0:
            continue
        costo_total = costo_unitario * unidades
        precio_ml = _calc_precio_objetivo(
            costo_total=costo_total,
            ganancia_factor=ganancia_factor,
            comision_ml=comision_ml,
            brackets=BRACKETS_PACK10,
        )
        filas.append(
            {
                "unidades_en_pack": unidades,
                "costo_total": costo_total,
                "precio_ml": precio_ml,
            }
        )
    return filas
