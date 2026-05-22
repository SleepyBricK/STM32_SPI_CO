#!/usr/bin/env python3
"""
GUI для просмотра и построения графиков по CSV экспорту измерения импеданса RHS2116.

Запуск: python3 impedance_csv_viewer.py
"""

import csv
import math
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import re

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False


def _parse_float(s, default=None):
    if s is None or s == "":
        return default
    try:
        s = str(s).replace(",", ".")
        return float(s)
    except (ValueError, TypeError):
        return default


def parse_impedance_csv(path):
    """Парсит CSV экспорт импеданса. Возвращает dict с секциями."""
    data = {
        "params": {},
        "auto_probes": [],
        "auto_adc": [],
        "raw_measurements": [],
        "raw_adc": [],
        "statistics": {},
        "valid_z": [],
        "dac_vals": [],
    }
    current_section = None
    headers = []

    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f, delimiter=";")
        for row in reader:
            if not row:
                continue
            first = row[0].strip() if row else ""
            if first.startswith("---"):
                m = re.search(r"---\s*(.+?)\s*---", first)
                current_section = m.group(1).strip() if m else first
                headers = []
                continue
            if first.startswith("#"):
                continue

            if current_section == "PARAMETRY IZMERENIYA":
                if len(row) >= 2 and row[0] and not row[0].startswith("parametr"):
                    key = row[0].strip()
                    val = _parse_float(row[1])
                    data["params"][key] = val if val is not None else row[1]

            elif current_section == "AVTO C PROBY DO VYBORA SHKALY":
                if headers and row[0] != "nom_proby":
                    if len(row) >= 5:
                        data["auto_probes"].append({
                            "nom": _parse_float(row[0], 0),
                            "scale": row[1] if len(row) > 1 else "",
                            "z_raw": _parse_float(row[2]),
                            "z_corr": _parse_float(row[3]),
                            "V_amp": _parse_float(row[4]),
                            "V_rms": _parse_float(row[5]) if len(row) > 5 else None,
                            "mean_dc": _parse_float(row[6]) if len(row) > 6 else None,
                        })
                elif row and row[0] == "nom_proby":
                    headers = row

            elif current_section == "AVTO C SYRYE ADC 64 tochek kazhdoy proby":
                if row and row[0] != "nom_proby":
                    if len(row) >= 5:
                        data["auto_adc"].append({
                            "nom_proby": _parse_float(row[0], 0),
                            "scale": row[1],
                            "sample_idx": _parse_float(row[2], 0),
                            "adc_raw": _parse_float(row[3], 0),
                            "value_uv": _parse_float(row[4]),
                        })

            elif current_section == "KAZHDYY OTDELNYY ZAMER do usredneniya":
                if row and row[0] != "nom_zamera":
                    if len(row) >= 4:
                        data["raw_measurements"].append({
                            "nom": _parse_float(row[0], 0),
                            "z_raw": _parse_float(row[1]),
                            "z_corr": _parse_float(row[2]),
                            "V_amp": _parse_float(row[3]),
                            "V_rms": _parse_float(row[4]) if len(row) > 4 else None,
                            "rejected": row[-2] == "da" if len(row) >= 9 else False,
                        })

            elif current_section == "SYRYE ADC 64 tochek kazhdogo zamera":
                if row and row[0] != "nom_zamera":
                    if len(row) >= 4:
                        data["raw_adc"].append({
                            "nom_zamera": _parse_float(row[0], 0),
                            "sample_idx": _parse_float(row[1], 0),
                            "adc_raw": _parse_float(row[2], 0),
                            "value_uv": _parse_float(row[3]),
                        })

            elif current_section == "STATISTIKA POSLE USREDNENIYA":
                if len(row) >= 2 and row[0] and not row[0].startswith("pokazatel"):
                    key = row[0].strip()
                    val = _parse_float(row[1])
                    data["statistics"][key] = val if val is not None else row[1]

            elif current_section == "SPISOK VALIDNYH ZNAMENIY voshli v raschet":
                if row and row[0] != "nom":
                    if len(row) >= 2:
                        data["valid_z"].append(_parse_float(row[1]))

            elif current_section == "DAC VALUES sine 64 tochki":
                if row and row[0] != "sample_idx":
                    if len(row) >= 2:
                        data["dac_vals"].append(_parse_float(row[1], 0))

    return data


class ImpedanceCsvViewer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Impedance CSV Viewer — RHS2116")
        self.geometry("900x700")
        self.data = None
        self.filepath = None

        if not MATPLOTLIB_AVAILABLE:
            messagebox.showerror("Ошибка", "Требуется matplotlib: pip install matplotlib")
            return

        main = ttk.Frame(self, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(main)
        top.pack(fill=tk.X, pady=(0, 5))
        ttk.Button(top, text="📂 Открыть CSV", command=self.open_file).pack(side=tk.LEFT, padx=(0, 5))
        self.lbl_file = ttk.Label(top, text="Файл не выбран", foreground="gray")
        self.lbl_file.pack(side=tk.LEFT)

        graphs_frame = ttk.LabelFrame(main, text="Графики", padding=5)
        graphs_frame.pack(fill=tk.X, pady=5)

        btn_row1 = ttk.Frame(graphs_frame)
        btn_row1.pack(fill=tk.X)
        ttk.Button(btn_row1, text="Импеданс по замерам (z_raw, z_corr)", command=self.plot_z_vs_iter).pack(side=tk.LEFT, padx=2, pady=2)
        ttk.Button(btn_row1, text="V_amp по замерам", command=self.plot_vamp_vs_iter).pack(side=tk.LEFT, padx=2, pady=2)
        ttk.Button(btn_row1, text="Гистограмма импеданса", command=self.plot_histogram).pack(side=tk.LEFT, padx=2, pady=2)
        ttk.Button(btn_row1, text="Box plot valid Z", command=self.plot_boxplot).pack(side=tk.LEFT, padx=2, pady=2)

        btn_row2 = ttk.Frame(graphs_frame)
        btn_row2.pack(fill=tk.X)
        ttk.Button(btn_row2, text="Авто C: Z и V по шкалам", command=self.plot_auto_probes).pack(side=tk.LEFT, padx=2, pady=2)
        ttk.Button(btn_row2, text="Синус ADC (выбор замера)", command=self.plot_adc_waveform).pack(side=tk.LEFT, padx=2, pady=2)
        ttk.Button(btn_row2, text="DAC sine 64 точки", command=self.plot_dac_sine).pack(side=tk.LEFT, padx=2, pady=2)
        self.spin_measurement = ttk.Spinbox(btn_row2, from_=1, to=20, width=4)
        self.spin_measurement.insert(0, "1")
        self.spin_measurement.pack(side=tk.LEFT, padx=2, pady=2)

        self.fig_frame = ttk.Frame(main)
        self.fig_frame.pack(fill=tk.BOTH, expand=True)
        self.canvas = None
        self.toolbar = None

    def open_file(self):
        path = filedialog.askopenfilename(
            title="Открыть CSV импеданса",
            filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")]
        )
        if not path:
            return
        try:
            self.data = parse_impedance_csv(path)
            self.filepath = path
            self.lbl_file.config(text=path.split("/")[-1].split("\\")[-1], foreground="black")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def _clear_plot(self):
        if self.canvas:
            self.canvas.get_tk_widget().destroy()
        if self.toolbar:
            self.toolbar.destroy()
        self.canvas = None
        self.toolbar = None

    def _show_plot(self, fig):
        self._clear_plot()
        self.canvas = FigureCanvasTkAgg(fig, master=self.fig_frame)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.toolbar = NavigationToolbar2Tk(self.canvas, self.fig_frame)
        self.toolbar.update()

    def _require_data(self):
        if not self.data:
            messagebox.showinfo("", "Сначала откройте CSV файл.")
            return False
        return True

    def plot_z_vs_iter(self):
        if not self._require_data():
            return
        raw = self.data.get("raw_measurements", [])
        if not raw:
            messagebox.showinfo("", "Нет данных raw_measurements.")
            return
        fig = Figure(figsize=(9, 5), dpi=100)
        ax = fig.add_subplot(111)
        valid_raw = [(r["nom"], r["z_raw"]) for r in raw if r.get("z_raw") is not None]
        valid_corr = [(r["nom"], r["z_corr"]) for r in raw if r.get("z_corr") is not None]
        valid_corr = [(n, z) for n, z in valid_corr if not (isinstance(z, float) and math.isnan(z))]
        noms_raw, z_raw = zip(*valid_raw) if valid_raw else ([], [])
        noms_corr, z_corr = zip(*valid_corr) if valid_corr else ([], [])
        noms_raw, z_raw = list(noms_raw), list(z_raw)
        noms_corr, z_corr = list(noms_corr), list(z_corr)

        # Масштаб: MΩ для значений > 100 kΩ, иначе кΩ или Ω
        z_all = list(z_raw) + list(z_corr)
        z_max = max(z_all) if z_all else 1
        z_min = min(z_all) if z_all else 0
        if z_max >= 1e6:
            scale = 1e6
            unit = "MΩ"
            z_raw_plot = [z / scale for z in z_raw]
            z_corr_plot = [z / scale for z in z_corr] if z_corr else []
        elif z_max >= 1e3:
            scale = 1e3
            unit = "кΩ"
            z_raw_plot = [z / scale for z in z_raw]
            z_corr_plot = [z / scale for z in z_corr] if z_corr else []
        else:
            scale = 1
            unit = "Ω"
            z_raw_plot = z_raw
            z_corr_plot = z_corr

        ax.plot(noms_raw, z_raw_plot, "o-", label="z_raw", markersize=5, alpha=0.7)
        if z_corr_plot:
            ax.plot(noms_corr, z_corr_plot, "s-", label="z_corrected", markersize=4, alpha=0.7)

        # Накопительное среднее (формирование среднего)
        if len(z_raw_plot) >= 2:
            cummean_raw = []
            s = 0
            for i, z in enumerate(z_raw_plot):
                s += z
                cummean_raw.append(s / (i + 1))
            ax.plot(noms_raw, cummean_raw, "-", label="Среднее z_raw", linewidth=2, color="blue")
        if len(z_corr_plot) >= 2:
            cummean_corr = []
            s = 0
            for i, z in enumerate(z_corr_plot):
                s += z
                cummean_corr.append(s / (i + 1))
            ax.plot(noms_corr, cummean_corr, "-", label="Среднее z_corr", linewidth=2, color="orange")

        ax.set_xlabel("Номер замера")
        ax.set_ylabel(f"Импеданс ({unit})")
        ax.set_title("Импеданс по замерам до усреднения и формирование среднего")
        ax.legend(loc="best", fontsize=9)
        ax.grid(True, alpha=0.3)
        ax.set_ylim(bottom=0)
        fig.tight_layout()
        self._show_plot(fig)

    def plot_vamp_vs_iter(self):
        if not self._require_data():
            return
        raw = self.data.get("raw_measurements", [])
        if not raw:
            messagebox.showinfo("", "Нет данных.")
            return
        fig = Figure(figsize=(8, 4), dpi=100)
        ax = fig.add_subplot(111)
        noms = [r["nom"] for r in raw]
        v_amp = [r["V_amp"] for r in raw if r.get("V_amp") is not None]
        valid_noms = [r["nom"] for r in raw if r.get("V_amp") is not None]
        ax.bar(valid_noms, v_amp, color="steelblue", alpha=0.7, label="V_amp (microV)")
        ax.axhline(y=250, color="orange", linestyle="--", label="250 microV (best)")
        ax.axhline(y=5, color="red", linestyle=":", label="5 microV (threshold)")
        ax.set_xlabel("Nomer zamera")
        ax.set_ylabel("V_amp (microV)")
        ax.set_title("Amplituda napryazheniya po zameram")
        ax.legend()
        ax.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        self._show_plot(fig)

    def plot_histogram(self):
        if not self._require_data():
            return
        z_list = self.data.get("valid_z", [])
        if not z_list:
            z_list = [r["z_corr"] for r in self.data.get("raw_measurements", []) if r.get("z_corr") is not None]
        z_list = [z for z in z_list if z is not None]
        if not z_list:
            messagebox.showinfo("", "Нет данных valid Z.")
            return
        fig = Figure(figsize=(8, 4), dpi=100)
        ax = fig.add_subplot(111)
        ax.hist(z_list, bins=min(15, len(z_list)), edgecolor="black", alpha=0.7)
        ax.axvline(x=sum(z_list) / len(z_list), color="red", linestyle="--", label="srednee")
        med = sorted(z_list)[len(z_list) // 2]
        ax.axvline(x=med, color="green", linestyle="--", label="mediana")
        ax.set_xlabel("Impedans (Ohm)")
        ax.set_ylabel("Chastota")
        ax.set_title("Gistogramma impedansa")
        ax.legend()
        ax.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        self._show_plot(fig)

    def plot_boxplot(self):
        if not self._require_data():
            return
        z_list = self.data.get("valid_z", [])
        if not z_list:
            z_list = [r["z_corr"] for r in self.data.get("raw_measurements", []) if r.get("z_corr") is not None]
        z_list = [z for z in z_list if z is not None]
        if not z_list:
            messagebox.showinfo("", "Нет данных.")
            return
        fig = Figure(figsize=(6, 4), dpi=100)
        ax = fig.add_subplot(111)
        bp = ax.boxplot([z_list], labels=["Z (Ohm)"], patch_artist=True)
        bp["boxes"][0].set_facecolor("lightblue")
        ax.set_ylabel("Impedans (Ohm)")
        ax.set_title("Box plot validnyh znacheniy")
        ax.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        self._show_plot(fig)

    def plot_auto_probes(self):
        if not self._require_data():
            return
        probes = self.data.get("auto_probes", [])
        if not probes:
            messagebox.showinfo("", "Нет данных AVTO C (izmerenie bez auto scale).")
            return
        fig, (ax1, ax2) = Figure(figsize=(9, 5), dpi=100).subplots(1, 2)
        scales = [p["scale"] for p in probes]
        z_raw = [p["z_raw"] for p in probes if p.get("z_raw") is not None]
        z_corr = [p["z_corr"] for p in probes if p.get("z_corr") is not None]
        v_amp = [p["V_amp"] for p in probes if p.get("V_amp") is not None]
        x = range(len(scales))
        ax1.bar([i - 0.2 for i in x], z_raw[:len(x)], 0.4, label="z_raw", color="steelblue")
        ax1.bar([i + 0.2 for i in x], z_corr[:len(x)], 0.4, label="z_corr", color="coral", alpha=0.8)
        ax1.set_xticks(x)
        ax1.set_xticklabels(scales)
        ax1.set_ylabel("Impedans (Ohm)")
        ax1.set_title("Avto C: impedans po shkalam")
        ax1.legend()
        ax1.grid(True, alpha=0.3, axis="y")
        ax2.bar(x, v_amp[:len(x)], color="seagreen", alpha=0.7)
        ax2.axhline(y=250, color="orange", linestyle="--", label="250 microV")
        ax2.set_xticks(x)
        ax2.set_xticklabels(scales)
        ax2.set_ylabel("V_amp (microV)")
        ax2.set_title("Avto C: amplituda po shkalam")
        ax2.legend()
        ax2.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        self._show_plot(fig)

    def plot_adc_waveform(self):
        if not self._require_data():
            return
        raw_adc = self.data.get("raw_adc", [])
        if not raw_adc:
            messagebox.showinfo("", "Нет syryh ADC dannyh.")
            return
        try:
            nom = int(self.spin_measurement.get())
        except ValueError:
            nom = 1
        by_meas = {}
        for r in raw_adc:
            nm = int(r.get("nom_zamera", 0))
            if nm not in by_meas:
                by_meas[nm] = []
            by_meas[nm].append(r)
        if nom not in by_meas:
            nom = min(by_meas.keys()) if by_meas else 1
        rows = sorted(by_meas.get(nom, []), key=lambda x: x.get("sample_idx", 0))
        idxs = [r["sample_idx"] for r in rows]
        uv = [r["value_uv"] for r in rows if r.get("value_uv") is not None]
        if not uv:
            uv = [0] * len(idxs)
        fig = Figure(figsize=(9, 4), dpi=100)
        ax = fig.add_subplot(111)
        ax.plot(idxs[:len(uv)], uv, "o-", markersize=3)
        ax.set_xlabel("Sample index (0-63)")
        ax.set_ylabel("Napryazhenie (microV)")
        ax.set_title("Syroy ADC zamer #{} (64 tochki sine)".format(nom))
        ax.grid(True, alpha=0.3)
        ax.axhline(y=0, color="gray", linestyle=":")
        fig.tight_layout()
        self._show_plot(fig)

    def plot_dac_sine(self):
        if not self._require_data():
            return
        dac = self.data.get("dac_vals", [])
        if not dac:
            messagebox.showinfo("", "Нет DAC values.")
            return
        fig = Figure(figsize=(8, 4), dpi=100)
        ax = fig.add_subplot(111)
        ax.plot(range(len(dac)), dac, "o-", markersize=4, color="purple")
        ax.set_xlabel("Sample index")
        ax.set_ylabel("DAC value")
        ax.set_title("DAC sine 64 tochki")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        self._show_plot(fig)


def main():
    app = ImpedanceCsvViewer()
    if MATPLOTLIB_AVAILABLE:
        app.mainloop()


if __name__ == "__main__":
    main()
