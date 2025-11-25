
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
import tkinter.font as tkfont

# Importa el motor de cálculo
try:
    import calculadora_milei_core as core
except ImportError as exc:
    raise SystemExit(
        "No se pudo importar 'calculadora_milei_core'.\n"
        "Copiá calculadora_milei_core.py a la misma carpeta que este archivo."
    ) from exc


def _parse_float(text: str, default: float | None = None) -> float:
    """Convierte texto a float aceptando coma o punto."""
    if text is None:
        if default is not None:
            return float(default)
        raise ValueError("Valor vacío")
    text = text.strip()
    if not text:
        if default is not None:
            return float(default)
        raise ValueError("Valor vacío")
    text = text.replace(",", ".")
    return float(text)


def _parse_int_list(text: str, default: tuple[int, ...]) -> list[int]:
    """Convierte '1,5,10' en [1, 5, 10]."""
    if text is None:
        return list(default)
    text = text.strip()
    if not text:
        return list(default)
    nums: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        nums.append(int(part))
    if not nums:
        return list(default)
    return nums


class MileiCalculatorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("CALCULADORA MILEI – MercadoLibre")
        # Gris/tema por defecto de Windows/Tk
        self.geometry("900x600")
        self.minsize(820, 540)

        # Data interna para precios base / costos
        self._sueltos_data: dict[str, dict] = {}
        self._packs_data: dict[str, dict] = {}
        # Valores actuales de Precio WEB (numéricos)
        self._precio_web_calc_val: float | None = None
        self._precio_web_set_val: float | None = None

        self._build_style()
        self._build_ui()

    def _build_style(self) -> None:
        style = ttk.Style(self)
        # Forzamos tema 'clam' para que respete los colores de los encabezados
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # Fuente un poco más grande
        style.configure("Header.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("SubHeader.TLabel", font=("Segoe UI", 11))
        style.configure("Label.TLabel", font=("Segoe UI", 11))
        style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"))
        style.configure("Secondary.TButton", font=("Segoe UI", 10))

        # Botón naranja para "CALCULAR TABLA"
        style.configure(
            "Accent.TButton",
            background="#f97316",  # naranja
            foreground="white",
            font=("Segoe UI", 11, "bold"),
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#ea580c"), ("pressed", "#c2410c")],
            foreground=[("active", "white"), ("pressed", "white")],
        )

        # Encabezados del Treeview: fondo negro, letras blancas bold
        style.configure(
            "Treeview.Heading",
            background="black",
            foreground="white",
            font=("Segoe UI", 11, "bold"),
            relief="raised",
        )
        style.map(
            "Treeview.Heading",
            background=[("active", "black"), ("pressed", "black")],
            foreground=[("active", "white"), ("pressed", "white")],
        )

        style.configure("Treeview", font=("Segoe UI", 11))

        # Fondo del LabelFrame de Precio WEB igual al resto
        try:
            bg = style.lookup("TFrame", "background") or style.lookup(".", "background")
        except Exception:
            bg = None
        if bg:
            style.configure("PrecioWeb.TLabelframe", background=bg)
            style.configure("PrecioWeb.TLabelframe.Label", background=bg)



    def _build_ui(self) -> None:
        header = ttk.Frame(self)
        header.pack(side=tk.TOP, fill=tk.X)

        lbl_title = ttk.Label(
            header,
            text="Calculadora Milei – ML",
            style="Header.TLabel",
        )
        lbl_title.pack(side=tk.TOP, anchor="w", padx=20, pady=(12, 0))

        lbl_sub = ttk.Label(
            header,
            text="Emulación de la planilla Calc ML MILEI.xlsx (artículos sueltos y packs).",
            style="SubHeader.TLabel",
        )
        lbl_sub.pack(side=tk.TOP, anchor="w", padx=20, pady=(0, 10))

        # ----- Búsqueda de artículo (código + descripción) -----
        frm_art = ttk.Frame(self)
        frm_art.pack(side=tk.TOP, fill=tk.X, padx=20, pady=(0, 6))

        ttk.Label(frm_art, text="Código de artículo:", style="Label.TLabel").pack(side=tk.LEFT)

        self.art_codigo_var = tk.StringVar()
        ent_codigo = ttk.Entry(frm_art, width=12, textvariable=self.art_codigo_var)
        ent_codigo.pack(side=tk.LEFT, padx=(4, 4))
        ent_codigo.bind("<Return>", self._on_buscar_articulo)

        btn_buscar = ttk.Button(frm_art, text="Buscar", command=self._on_buscar_articulo)
        btn_buscar.pack(side=tk.LEFT, padx=(4, 10))

        ttk.Label(frm_art, text="Descripción:", style="Label.TLabel").pack(side=tk.LEFT, padx=(10, 4))

        self.art_desc_var = tk.StringVar(value="")
        lbl_desc = ttk.Label(frm_art, textvariable=self.art_desc_var, style="Label.TLabel")
        lbl_desc.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ----- Tree de Precio WEB (antes del s.t actual) -----
        frm_pw = ttk.LabelFrame(self, text="Precio WEB", style="PrecioWeb.TLabelframe")
        frm_pw.pack(side=tk.TOP, fill=tk.X, padx=20, pady=(0, 8))

        cols_pw = ("precio_web_calc", "precio_web_set", "ganancia_web")
        self.tree_precio_web = ttk.Treeview(frm_pw, columns=cols_pw, show="headings", height=1)

        self.tree_precio_web.heading("precio_web_calc", text="PRECIO WEB CALCULADO")
        self.tree_precio_web.heading("precio_web_set", text="PRECIO WEB SETEAR")
        self.tree_precio_web.heading("ganancia_web", text="Ganancia %")

        self.tree_precio_web.column("precio_web_calc", width=200, anchor="e")
        self.tree_precio_web.column("precio_web_set", width=200, anchor="e")
        self.tree_precio_web.column("ganancia_web", width=120, anchor="e")

        self.tree_precio_web.pack(side=tk.TOP, fill=tk.X, padx=4, pady=4)

        # fila inicial vacía
        self.tree_precio_web.insert("", "end", values=("", "", ""))

        notebook = ttk.Notebook(self)
        notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=14, pady=(0, 8))

        self.tab_sueltos = ttk.Frame(notebook)
        self.tab_packs = ttk.Frame(notebook)

        notebook.add(self.tab_sueltos, text="Artículos sueltos")
        notebook.add(self.tab_packs, text="Pack x 10")

        self._build_tab_sueltos(self.tab_sueltos)
        self._build_tab_packs(self.tab_packs)

        self.status_var = tk.StringVar(value="Listo.")
        status = ttk.Label(self,
                           textvariable=self.status_var,
                           anchor="w",
                           style="SubHeader.TLabel")
        status.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 4))

    # ------------------------ util: autosize columnas ----------------------

    def _autosize_tree_columns(self, tree: ttk.Treeview) -> None:
        """Ajusta el ancho de las columnas al contenido (encabezado + filas)."""
        font = tkfont.nametofont("TkDefaultFont")
        for col in tree["columns"]:
            header_text = tree.heading(col, "text")
            width = font.measure(header_text)

            for item in tree.get_children():
                val = tree.set(item, col)
                text = str(val)
                w = font.measure(text)
                if w > width:
                    width = w
            # un poco de padding
            tree.column(col, width=width + 24)

    def _aplicar_descuento_item(
        self,
        item_id: str,
        data_dict: dict,
        descuento_var: tk.StringVar,
        tree: ttk.Treeview,
        price_col: str,
        ganancia_col: str,
        comision_ml: float,
        brackets,
    ) -> None:
        """Aplica el descuento actual del radio button al ítem indicado y recalcula ganancia %."""
        data = data_dict.get(item_id)
        if not data:
            return

        val = descuento_var.get()
        if val == "None":
            pct = 0.0
        else:
            try:
                pct = float(val)
            except ValueError:
                pct = 0.0

        data["descuento_pct"] = pct
        base = data["precio_ml_base"]
        nuevo = base * (1.0 - pct / 100.0)
        nuevo = max(0.0, nuevo)
        nuevo_round = round(nuevo)
        precio_str = f"{nuevo_round:,}".replace(",", ".")
        tree.set(item_id, price_col, precio_str)

        # Recalcular ganancia % para el nuevo precio
        costo_total = data.get("costo_total", 0.0)
        try:
            desg = core.desglose_venta(
                precio_venta=nuevo,
                costo_total=costo_total,
                comision_ml=comision_ml,
                brackets=brackets,
            )
            if costo_total > 0:
                gan_pct = (desg.ganancia_neta / costo_total) * 100.0
            else:
                gan_pct = 0.0
        except Exception:
            gan_pct = 0.0

        ganancia_str = (
            f"{gan_pct:,.1f}"
            .replace(",", "X")
            .replace(".", ",")
            .replace("X", ".")
        ) + " %"
        tree.set(item_id, ganancia_col, ganancia_str)


    # ------------------------ BÚSQUEDA DE ARTÍCULO -------------------------

    def _on_buscar_articulo(self, event: object | None = None) -> None:
        """
        Pide el código de artículo y, si existe en la tabla 'articulo' de
        C:\!GESTION2026\gestion.sqlite3, muestra la descripción en la barra
        superior. No modifica ninguna lógica de cálculo existente.
        """
        codigo = (self.art_codigo_var.get() or "").strip()
        if not codigo:
            messagebox.showwarning("Código vacío", "Ingresá un código de artículo.")
            return

        conn = None
        try:
            import sqlite3

            conn = sqlite3.connect(r"C:\!GESTION2026\gestion.sqlite3")
            cur = conn.cursor()

            # Descubro columnas disponibles en 'articulo'
            cur.execute("PRAGMA table_info(articulo)")
            cols_info = cur.fetchall()
            col_names = [c[1] for c in cols_info]

            # Busco una columna de descripción razonable
            desc_candidates = ["descripcion", "detalle", "descrip", "nombre"]
            desc_col = next((c for c in desc_candidates if c in col_names), None)

            if not desc_col:
                self.art_desc_var.set("(no hay columna de descripción en 'articulo')")
                return

            sql = f"SELECT {desc_col} FROM articulo WHERE codigo = ?"
            cur.execute(sql, (codigo,))
            row = cur.fetchone()

            if row:
                self.art_desc_var.set(str(row[0]))
            else:
                self.art_desc_var.set("(código no encontrado)")
        except Exception as exc:
            self.art_desc_var.set("(error al consultar la base)")
            messagebox.showerror(
                "Error de base de datos",
                f"No se pudo consultar la tabla 'articulo':\n{exc}",
            )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


    # ------------------------ TAB: SUELTOS ---------------------------------

    def _build_tab_sueltos(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        top = ttk.Frame(parent)
        top.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        frm1 = ttk.Frame(top)
        frm1.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))

        ttk.Label(frm1, text="Costo unitario:", style="Label.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        self.ent_costo_unitario = ttk.Entry(frm1, width=14)
        self.ent_costo_unitario.grid(row=0, column=1, sticky="w")
        self.ent_costo_unitario.insert(0, "0")

        ttk.Label(frm1, text="Ganancia %:", style="Label.TLabel").grid(
            row=0, column=2, sticky="w", padx=(18, 6)
        )
        self.ent_ganancia_sueltos = ttk.Entry(frm1, width=10)
        self.ent_ganancia_sueltos.grid(row=0, column=3, sticky="w")
        # 100% de ganancia = factor 1.0
        self.ent_ganancia_sueltos.insert(0, "100")

        ttk.Label(frm1, text="Comisión ML:", style="Label.TLabel").grid(
            row=0, column=4, sticky="w", padx=(18, 6)
        )
        self.ent_comision_sueltos = ttk.Entry(frm1, width=8)
        self.ent_comision_sueltos.grid(row=0, column=5, sticky="w")
        self.ent_comision_sueltos.insert(0, "0.16")

        frm2 = ttk.Frame(top)
        frm2.pack(side=tk.TOP, fill=tk.X, pady=(4, 0))

        ttk.Label(frm2, text="Cantidades (coma):", style="Label.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        self.ent_cantidades = ttk.Entry(frm2, width=40)
        self.ent_cantidades.grid(row=0, column=1, sticky="w")
        # Default ampliado
        self.ent_cantidades.insert(0, "1,5,10,15,20,25,50,100")

        btn_calc = ttk.Button(
            frm2, text="CALCULAR TABLA", style="Accent.TButton",
            command=self._on_calc_sueltos,
        )
        btn_calc.grid(row=0, column=2, sticky="w", padx=(16, 4))

        btn_clear = ttk.Button(
            frm2, text="Limpiar", style="Secondary.TButton",
            command=self._on_clear_sueltos,
        )
        btn_clear.grid(row=0, column=3, sticky="w")

        frame_table = ttk.Frame(parent)
        frame_table.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        frame_table.rowconfigure(0, weight=1)
        frame_table.columnconfigure(0, weight=1)

        # Columna 1: checkbox, luego cantidad / costo / precio / ganancia%
        columns = ("sel", "cantidad", "costo_total", "precio_ml", "ganancia_pct")
        self.tree_sueltos = ttk.Treeview(
            frame_table,
            columns=columns,
            show="headings",
        )
        self.tree_sueltos.heading("sel", text="SEL")
        self.tree_sueltos.heading("cantidad", text="Cant.")
        self.tree_sueltos.heading("costo_total", text="Costo total")
        self.tree_sueltos.heading("precio_ml", text="Precio ML")
        self.tree_sueltos.heading("ganancia_pct", text="Ganancia %")

        self.tree_sueltos.column("sel", width=50, anchor="center")
        self.tree_sueltos.column("cantidad", anchor="center")
        self.tree_sueltos.column("costo_total", anchor="e")
        self.tree_sueltos.column("precio_ml", anchor="e")
        self.tree_sueltos.column("ganancia_pct", anchor="e")

        vsb = ttk.Scrollbar(frame_table, orient="vertical", command=self.tree_sueltos.yview)
        self.tree_sueltos.configure(yscrollcommand=vsb.set)

        self.tree_sueltos.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        # Zebra amarillo
        self.tree_sueltos.tag_configure("odd", background="#fff9c4")   # amarillo suave
        self.tree_sueltos.tag_configure("even", background="#fffde7")  # casi blanco amarillento

        # Click en la columna SEL para togglear el "checkbox"
        self.tree_sueltos.bind("<Button-1>", self._on_tree_click_sueltos)
        # Cambio de selección (por si seleccionás clickeando otra columna)
        self.tree_sueltos.bind("<<TreeviewSelect>>", self._on_tree_select_sueltos)

        frame_actions = ttk.Frame(parent)
        frame_actions.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))

        btn_desglose = ttk.Button(
            frame_actions,
            text="Ver desglose de fila seleccionada",
            style="Secondary.TButton",
            command=self._on_desglose_sueltos,
        )
        btn_desglose.pack(side=tk.LEFT)

        # Radio buttons de descuento
        self.descuento_var_sueltos = tk.StringVar(value="None")
        frm_rad = ttk.Frame(frame_actions)
        frm_rad.pack(side=tk.RIGHT)

        ttk.Label(frm_rad, text="Descuento:", style="Label.TLabel").pack(side=tk.LEFT, padx=(0, 4))

        for val in ["None", "2", "3", "5", "7", "10", "20"]:
            text = f"{val}%" if val != "None" else "None"
            rb = ttk.Radiobutton(
                frm_rad,
                text=text,
                value=val,
                variable=self.descuento_var_sueltos,
                command=self._on_descuento_change_sueltos,
            )
            rb.pack(side=tk.LEFT, padx=2)

    def _on_tree_click_sueltos(self, event: tk.Event) -> None:
        """Toggle de checkbox de la columna SEL y aplica descuento si corresponde."""
        region = self.tree_sueltos.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.tree_sueltos.identify_column(event.x)  # '#1', '#2', ...
        row_id = self.tree_sueltos.identify_row(event.y)
        if not row_id:
            return

        try:
            comision_ml = _parse_float(self.ent_comision_sueltos.get(), default=0.16)
        except ValueError:
            comision_ml = 0.16

        if col == "#1":
            current = self.tree_sueltos.set(row_id, "sel")
            new_val = "☑" if current != "☑" else "☐"
            self.tree_sueltos.set(row_id, "sel", new_val)

            # Si destildo, vuelvo a precio base (sin descuento)
            if new_val == "☐":
                # Fuerzo descuento None solo para este item
                original = self.descuento_var_sueltos.get()
                self.descuento_var_sueltos.set("None")
                self._aplicar_descuento_item(
                    row_id,
                    self._sueltos_data,
                    self.descuento_var_sueltos,
                    self.tree_sueltos,
                    "precio_ml",
                    "ganancia_pct",
                    comision_ml,
                    core.BRACKETS_SUELTOS,
                )
                self.descuento_var_sueltos.set(original)
            else:
                # Si lo tildo, aplico el descuento actual
                self._aplicar_descuento_item(
                    row_id,
                    self._sueltos_data,
                    self.descuento_var_sueltos,
                    self.tree_sueltos,
                    "precio_ml",
                    "ganancia_pct",
                    comision_ml,
                    core.BRACKETS_SUELTOS,
                )

        # seleccionamos la fila, pero el descuento depende del SEL
        self.tree_sueltos.selection_set(row_id)

    def _on_tree_select_sueltos(self, event: tk.Event) -> None:
        # Si sólo seleccionás (sin tocar SEL) no cambiamos nada
        # El descuento se maneja con los checkboxes y los radio buttons.
        return

    def _on_calc_sueltos(self) -> None:
        try:
            costo_unitario = _parse_float(self.ent_costo_unitario.get())
            ganancia_pct = _parse_float(self.ent_ganancia_sueltos.get(), default=100.0)
            ganancia_factor = ganancia_pct / 100.0
            comision_ml = _parse_float(self.ent_comision_sueltos.get(), default=0.16)
            cantidades = _parse_int_list(self.ent_cantidades.get(), default=(1, 5, 10, 15, 20, 25, 50, 100))
        except ValueError as e:
            messagebox.showerror("Error en datos", f"Revisá los valores ingresados: {e}")
            return

        try:
            tabla = core.tabla_sueltos(
                costo_unitario=costo_unitario,
                ganancia_factor=ganancia_factor,
                comision_ml=comision_ml,
                cantidades=cantidades,
            )
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Error de cálculo", f"Ocurrió un error al calcular:\n{e}")
            return

        # Limpiamos tabla y data interna
        for item in self.tree_sueltos.get_children():
            self.tree_sueltos.delete(item)
        self._sueltos_data.clear()

        for idx, fila in enumerate(tabla):
            tag = "odd" if idx % 2 == 0 else "even"
            costo_total = fila["costo_total"]

            # Costo con separador de miles y coma decimal: 12.345,67
            costo_str = (
                f"{costo_total:,.2f}"
                .replace(",", "X")
                .replace(".", ",")
                .replace("X", ".")
            )
            # Precio ML base (sin redondear), luego lo mostramos redondeado
            precio_base = fila["precio_ml"]
            precio_round = round(precio_base)
            precio_str = f"{precio_round:,}".replace(",", ".")

            # Ganancia % con respecto al costo (usando precio_base)
            try:
                desg = core.desglose_venta(
                    precio_venta=precio_base,
                    costo_total=costo_total,
                    comision_ml=comision_ml,
                    brackets=core.BRACKETS_SUELTOS,
                )
                if costo_total > 0:
                    gan_pct = (desg.ganancia_neta / costo_total) * 100.0
                else:
                    gan_pct = 0.0
            except Exception:
                gan_pct = 0.0

            ganancia_str = (
                f"{gan_pct:,.1f}"
                .replace(",", "X")
                .replace(".", ",")
                .replace("X", ".")
            ) + " %"

            item_id = self.tree_sueltos.insert(
                "",
                "end",
                values=(
                    "☐",
                    fila["cantidad"],
                    costo_str,
                    precio_str,
                    ganancia_str,
                ),
                tags=(tag,),
            )
            self._sueltos_data[item_id] = {
                "cantidad": fila["cantidad"],
                "costo_total": costo_total,
                "precio_ml_base": precio_base,
                "descuento_pct": 0.0,
            }

        
        # Calcular PRECIO WEB CALCULADO a partir de la fila de cantidad 1
        precio_web_calc = None
        try:
            # Buscar en la tabla de cálculo la fila con cantidad 1
            for fila in tabla:
                if fila.get("cantidad") == 1:
                    precio_ml_1 = fila["precio_ml"]
                    try:
                        desg_web = core.desglose_venta(
                            precio_venta=precio_ml_1,
                            costo_total=0.0,
                            comision_ml=comision_ml,
                            brackets=core.BRACKETS_SUELTOS,
                        )
                        costo_fijo = desg_web.comision_fija
                    except Exception:
                        costo_fijo = 0.0
                    precio_web_calc = (precio_ml_1 - costo_fijo) * 0.90
                    if precio_web_calc < 0:
                        precio_web_calc = 0.0
                    break
        except Exception:
            precio_web_calc = None

        # Actualizamos el s.t de Precio WEB (por ahora sin valor seteado)
        self._update_precio_web(precio_web_calc, None)

# Ajuste perfecto de columnas según contenido
        self._autosize_tree_columns(self.tree_sueltos)

        self.status_var.set(
            f"Sueltos: costo unitario={costo_unitario:.2f}, "
            f"ganancia={ganancia_pct:.1f}%, comisión ML={comision_ml:.3f}."
        )

    def _on_clear_sueltos(self) -> None:
        self.ent_costo_unitario.delete(0, tk.END)
        self.ent_costo_unitario.insert(0, "0")
        self.ent_ganancia_sueltos.delete(0, tk.END)
        self.ent_ganancia_sueltos.insert(0, "100")
        self.ent_comision_sueltos.delete(0, tk.END)
        self.ent_comision_sueltos.insert(0, "0.16")
        for item in self.tree_sueltos.get_children():
            self.tree_sueltos.delete(item)
        self._sueltos_data.clear()
        self.descuento_var_sueltos.set("None")
        self.status_var.set("Sueltos: limpiado.")


    def _update_precio_web(self, precio_calc: float | None, precio_set: float | None) -> None:
        """Actualiza el s.t de Precio WEB y la Ganancia % (markup)."""
        self._precio_web_calc_val = precio_calc
        self._precio_web_set_val = precio_set

        # Limpiar filas actuales
        for item in self.tree_precio_web.get_children():
            self.tree_precio_web.delete(item)

        # Formateo de números al estilo resto de la calculadora
        def _fmt_precio(v: float | None) -> str:
            if v is None:
                return ""
            try:
                v_round = round(v)
            except Exception:
                return ""
            return f"{v_round:,}".replace(",", ".")

        # Determinar base para ganancia % (set > 0, si no calc)
        base_precio = None
        if precio_set is not None and precio_set > 0:
            base_precio = precio_set
        elif precio_calc is not None and precio_calc > 0:
            base_precio = precio_calc

        ganancia_str = ""
        costo_unit = _parse_float(self.ent_costo_unitario.get(), default=0.0)
        if base_precio is not None and costo_unit and costo_unit > 0:
            try:
                markup = (base_precio / costo_unit - 1.0) * 100.0
            except Exception:
                markup = 0.0
            ganancia_str = (
                f"{markup:,.1f}"
                .replace(",", "X")
                .replace(".", ",")
                .replace("X", ".")
            ) + " %"

        self.tree_precio_web.insert(
            "",
            "end",
            values=(
                _fmt_precio(precio_calc),
                _fmt_precio(precio_set),
                ganancia_str,
            ),
        )

    def _on_descuento_change_sueltos(self) -> None:
        """Aplica el descuento elegido a TODAS las filas tildadas en SEL."""
        try:
            comision_ml = _parse_float(self.ent_comision_sueltos.get(), default=0.16)
        except ValueError:
            comision_ml = 0.16

        for item_id in self.tree_sueltos.get_children():
            if self.tree_sueltos.set(item_id, "sel") == "☑":
                self._aplicar_descuento_item(
                    item_id,
                    self._sueltos_data,
                    self.descuento_var_sueltos,
                    self.tree_sueltos,
                    "precio_ml",
                    "ganancia_pct",
                    comision_ml,
                    core.BRACKETS_SUELTOS,
                )

    def _on_desglose_sueltos(self) -> None:
        sel = self.tree_sueltos.selection()
        if not sel:
            messagebox.showinfo("Desglose", "Seleccioná una fila de la tabla primero.")
            return

        item_id = sel[0]
        vals = self.tree_sueltos.item(item_id, "values")
        if len(vals) < 4:
            return

        try:
            # Precio ML viene como "25.000"
            precio_venta = float(str(vals[3]).replace(".", ""))
            # Costo total viene como "12.345,67"
            costo_total = float(
                str(vals[2])
                .replace(".", "")
                .replace(",", ".")
            )
            comision_ml = _parse_float(self.ent_comision_sueltos.get(), default=0.16)
        except ValueError:
            messagebox.showerror("Error", "No se pudo interpretar los valores seleccionados.")
            return

        desg = core.desglose_venta(
            precio_venta=precio_venta,
            costo_total=costo_total,
            comision_ml=comision_ml,
            brackets=core.BRACKETS_SUELTOS,
        )
        self._show_desglose_window(desg, titulo="Desglose – Artículo suelto")

    # ------------------------ TAB: PACKS -----------------------------------

    def _build_tab_packs(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        top = ttk.Frame(parent)
        top.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        frm1 = ttk.Frame(top)
        frm1.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))

        ttk.Label(frm1, text="Costo PACK x 10 (C6):", style="Label.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        self.ent_costo_pack10 = ttk.Entry(frm1, width=14)
        self.ent_costo_pack10.grid(row=0, column=1, sticky="w")
        self.ent_costo_pack10.insert(0, "0")

        ttk.Label(frm1, text="Ganancia %:", style="Label.TLabel").grid(
            row=0, column=2, sticky="w", padx=(18, 6)
        )
        self.ent_ganancia_packs = ttk.Entry(frm1, width=10)
        self.ent_ganancia_packs.grid(row=0, column=3, sticky="w")
        # por default 100 %
        self.ent_ganancia_packs.insert(0, "100")

        ttk.Label(frm1, text="Comisión ML:", style="Label.TLabel").grid(
            row=0, column=4, sticky="w", padx=(18, 6)
        )
        self.ent_comision_packs = ttk.Entry(frm1, width=8)
        self.ent_comision_packs.grid(row=0, column=5, sticky="w")
        self.ent_comision_packs.insert(0, "0.16")

        frm2 = ttk.Frame(top)
        frm2.pack(side=tk.TOP, fill=tk.X, pady=(4, 0))

        ttk.Label(frm2, text="Unidades por pack (coma):", style="Label.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        self.ent_unidades_pack = ttk.Entry(frm2, width=40)
        self.ent_unidades_pack.grid(row=0, column=1, sticky="w")
        self.ent_unidades_pack.insert(0, "2,3,4,5,10,50,100,150,200,250")

        btn_calc = ttk.Button(
            frm2, text="CALCULAR TABLA", style="Accent.TButton",
            command=self._on_calc_packs,
        )
        btn_calc.grid(row=0, column=2, sticky="w", padx=(16, 4))

        btn_clear = ttk.Button(
            frm2, text="Limpiar", style="Secondary.TButton",
            command=self._on_clear_packs,
        )
        btn_clear.grid(row=0, column=3, sticky="w")

        frame_table = ttk.Frame(parent)
        frame_table.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        frame_table.rowconfigure(0, weight=1)
        frame_table.columnconfigure(0, weight=1)

        columns = ("sel", "unidades_en_pack", "costo_total", "precio_ml", "ganancia_pct")
        self.tree_packs = ttk.Treeview(
            frame_table,
            columns=columns,
            show="headings",
        )
        self.tree_packs.heading("sel", text="SEL")
        self.tree_packs.heading("unidades_en_pack", text="Unidades pack")
        self.tree_packs.heading("costo_total", text="Costo total")
        self.tree_packs.heading("precio_ml", text="Precio ML")
        self.tree_packs.heading("ganancia_pct", text="Ganancia %")

        self.tree_packs.column("sel", width=50, anchor="center")
        self.tree_packs.column("unidades_en_pack", anchor="center")
        self.tree_packs.column("costo_total", anchor="e")
        self.tree_packs.column("precio_ml", anchor="e")
        self.tree_packs.column("ganancia_pct", anchor="e")

        vsb = ttk.Scrollbar(frame_table, orient="vertical", command=self.tree_packs.yview)
        self.tree_packs.configure(yscrollcommand=vsb.set)

        self.tree_packs.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        # Zebra amarillo
        self.tree_packs.tag_configure("odd", background="#fff9c4")
        self.tree_packs.tag_configure("even", background="#fffde7")

        # Click en checkbox y selección
        self.tree_packs.bind("<Button-1>", self._on_tree_click_packs)
        self.tree_packs.bind("<<TreeviewSelect>>", self._on_tree_select_packs)

        frame_actions = ttk.Frame(parent)
        frame_actions.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))

        btn_desglose = ttk.Button(
            frame_actions,
            text="Ver desglose de fila seleccionada",
            style="Secondary.TButton",
            command=self._on_desglose_packs,
        )
        btn_desglose.pack(side=tk.LEFT)

        # Radio buttons de descuento para packs
        self.descuento_var_packs = tk.StringVar(value="None")
        frm_rad = ttk.Frame(frame_actions)
        frm_rad.pack(side=tk.RIGHT)

        ttk.Label(frm_rad, text="Descuento:", style="Label.TLabel").pack(side=tk.LEFT, padx=(0, 4))

        for val in ["None", "2", "3", "5", "7", "10", "20"]:
            text = f"{val}%" if val != "None" else "None"
            rb = ttk.Radiobutton(
                frm_rad,
                text=text,
                value=val,
                variable=self.descuento_var_packs,
                command=self._on_descuento_change_packs,
            )
            rb.pack(side=tk.LEFT, padx=2)

    def _on_tree_click_packs(self, event: tk.Event) -> None:
        region = self.tree_packs.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.tree_packs.identify_column(event.x)
        row_id = self.tree_packs.identify_row(event.y)
        if not row_id:
            return

        try:
            comision_ml = _parse_float(self.ent_comision_packs.get(), default=0.16)
        except ValueError:
            comision_ml = 0.16

        if col == "#1":
            current = self.tree_packs.set(row_id, "sel")
            new_val = "☑" if current != "☑" else "☐"
            self.tree_packs.set(row_id, "sel", new_val)

            if new_val == "☐":
                original = self.descuento_var_packs.get()
                self.descuento_var_packs.set("None")
                self._aplicar_descuento_item(
                    row_id,
                    self._packs_data,
                    self.descuento_var_packs,
                    self.tree_packs,
                    "precio_ml",
                    "ganancia_pct",
                    comision_ml,
                    core.BRACKETS_PACK10,
                )
                self.descuento_var_packs.set(original)
            else:
                self._aplicar_descuento_item(
                    row_id,
                    self._packs_data,
                    self.descuento_var_packs,
                    self.tree_packs,
                    "precio_ml",
                    "ganancia_pct",
                    comision_ml,
                    core.BRACKETS_PACK10,
                )

        self.tree_packs.selection_set(row_id)

    def _on_tree_select_packs(self, event: tk.Event) -> None:
        return

    def _on_calc_packs(self) -> None:
        try:
            costo_pack10 = _parse_float(self.ent_costo_pack10.get())
            ganancia_pct = _parse_float(self.ent_ganancia_packs.get(), default=100.0)
            ganancia_factor = ganancia_pct / 100.0
            comision_ml = _parse_float(self.ent_comision_packs.get(), default=0.16)
            unidades_list = _parse_int_list(
                self.ent_unidades_pack.get(),
                default=(2, 3, 4, 5, 10, 50, 100, 150, 200, 250),
            )
        except ValueError as e:
            messagebox.showerror("Error en datos", f"Revisá los valores ingresados: {e}")
            return

        try:
            tabla = core.tabla_pack_x10(
                costo_pack_10=costo_pack10,
                ganancia_factor=ganancia_factor,
                comision_ml=comision_ml,
                unidades_en_pack=unidades_list,
            )
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Error de cálculo", f"Ocurrió un error al calcular:\n{e}")
            return

        for item in self.tree_packs.get_children():
            self.tree_packs.delete(item)
        self._packs_data.clear()

        for idx, fila in enumerate(tabla):
            tag = "odd" if idx % 2 == 0 else "even"
            costo_total = fila["costo_total"]
            costo_str = (
                f"{costo_total:,.2f}"
                .replace(",", "X")
                .replace(".", ",")
                .replace("X", ".")
            )
            precio_base = fila["precio_ml"]
            precio_round = round(precio_base)
            precio_str = f"{precio_round:,}".replace(",", ".")

            try:
                desg = core.desglose_venta(
                    precio_venta=precio_base,
                    costo_total=costo_total,
                    comision_ml=comision_ml,
                    brackets=core.BRACKETS_PACK10,
                )
                if costo_total > 0:
                    gan_pct = (desg.ganancia_neta / costo_total) * 100.0
                else:
                    gan_pct = 0.0
            except Exception:
                gan_pct = 0.0

            ganancia_str = (
                f"{gan_pct:,.1f}"
                .replace(",", "X")
                .replace(".", ",")
                .replace("X", ".")
            ) + " %"

            item_id = self.tree_packs.insert(
                "",
                "end",
                values=(
                    "☐",
                    fila["unidades_en_pack"],
                    costo_str,
                    precio_str,
                    ganancia_str,
                ),
                tags=(tag,),
            )
            self._packs_data[item_id] = {
                "unidades_en_pack": fila["unidades_en_pack"],
                "costo_total": costo_total,
                "precio_ml_base": precio_base,
                "descuento_pct": 0.0,
            }

        self._autosize_tree_columns(self.tree_packs)

        self.status_var.set(
            f"Packs: costo pack x10={costo_pack10:.2f}, "
            f"ganancia={ganancia_pct:.1f}%, comisión ML={comision_ml:.3f}."
        )

    def _on_clear_packs(self) -> None:
        self.ent_costo_pack10.delete(0, tk.END)
        self.ent_costo_pack10.insert(0, "0")
        self.ent_ganancia_packs.delete(0, tk.END)
        self.ent_ganancia_packs.insert(0, "100")
        self.ent_comision_packs.delete(0, tk.END)
        self.ent_comision_packs.insert(0, "0.16")
        for item in self.tree_packs.get_children():
            self.tree_packs.delete(item)
        self._packs_data.clear()
        self.descuento_var_packs.set("None")
        self.status_var.set("Packs: limpiado.")

    def _on_descuento_change_packs(self) -> None:
        try:
            comision_ml = _parse_float(self.ent_comision_packs.get(), default=0.16)
        except ValueError:
            comision_ml = 0.16

        for item_id in self.tree_packs.get_children():
            if self.tree_packs.set(item_id, "sel") == "☑":
                self._aplicar_descuento_item(
                    item_id,
                    self._packs_data,
                    self.descuento_var_packs,
                    self.tree_packs,
                    "precio_ml",
                    "ganancia_pct",
                    comision_ml,
                    core.BRACKETS_PACK10,
                )

    def _on_desglose_packs(self) -> None:
        sel = self.tree_packs.selection()
        if not sel:
            messagebox.showinfo("Desglose", "Seleccioná una fila de la tabla primero.")
            return

        item_id = sel[0]
        vals = self.tree_packs.item(item_id, "values")
        if len(vals) < 4:
            return

        try:
            precio_venta = float(str(vals[3]).replace(".", ""))
            costo_total = float(
                str(vals[2])
                .replace(".", "")
                .replace(",", ".")
            )
            comision_ml = _parse_float(self.ent_comision_packs.get(), default=0.16)
        except ValueError:
            messagebox.showerror("Error", "No se pudo interpretar los valores seleccionados.")
            return

        desg = core.desglose_venta(
            precio_venta=precio_venta,
            costo_total=costo_total,
            comision_ml=comision_ml,
            brackets=core.BRACKETS_PACK10,
        )
        self._show_desglose_window(desg, titulo="Desglose – Pack")

    # --------------------------- DESGLOSE ----------------------------------

    def _show_desglose_window(self, desg: "core.DesgloseVenta", titulo: str) -> None:
        win = tk.Toplevel(self)
        win.title(titulo)
        win.geometry("400x320")  # un poco más alta para ver bien el botón Cerrar
        win.resizable(False, False)

        frm = ttk.Frame(win)
        frm.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        def add_row(row: int, label: str, value: float) -> None:
            ttk.Label(frm, text=label, style="Label.TLabel").grid(
                row=row, column=0, sticky="w", pady=4
            )
            ttk.Label(
                frm,
                text=(
                    f"{value:,.2f}"
                    .replace(",", "X")
                    .replace(".", ",")
                    .replace("X", ".")
                ),
                style="Label.TLabel"
            ).grid(
                row=row, column=1, sticky="e", pady=4
            )

        add_row(0, "Precio de venta:", desg.precio_venta)
        add_row(1, "Costo total:", desg.costo_total)
        add_row(2, "Comisión variable:", desg.comision_variable)
        add_row(3, "Comisión fija:", desg.comision_fija)
        add_row(4, "Limpio:", desg.limpio)
        add_row(5, "Ganancia neta:", desg.ganancia_neta)

        ttk.Separator(frm, orient="horizontal").grid(row=6, column=0, columnspan=2, sticky="ew", pady=(10, 8))

        btn_close = ttk.Button(frm, text="Cerrar", style="Secondary.TButton", command=win.destroy)
        btn_close.grid(row=7, column=0, columnspan=2, pady=(4, 0))

        for i in range(2):
            frm.columnconfigure(i, weight=1)

        win.transient(self)
        win.grab_set()
        win.focus_set()


def main() -> None:
    app = MileiCalculatorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
