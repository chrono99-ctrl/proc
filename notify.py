#!/usr/bin/env python3
"""notify.py — Manda un aviso al teléfono (app ntfy) con el resumen de una corrida.

Uso (en GitHub Actions):  python notify.py --video video07 --etapa principal --resultado success --carpeta reportes
Lee los reporte_XX.md de la carpeta. Si no hay NTFY_TOPIC (secreto del repo), no hace nada.
"""
import argparse
import os
import re
import sys
from pathlib import Path

import requests

NOMBRES = {"de": "alemán", "fr": "francés", "ja": "japonés", "ru": "ruso", "en": "inglés", "pt": "portugués",
           "ar": "árabe", "id": "indonesio", "vi": "vietnamita", "it": "italiano", "ko": "coreano", "tr": "turco"}


def dato(texto, patron):
    m = re.search(patron, texto)
    return m.group(1) if m else None


def resumen_idiomas(carpeta, pedidos):
    lineas, total_usd, hechos = [], 0.0, []
    for idioma in pedidos:
        ruta = carpeta / f"reporte_{idioma}.md"
        nombre = NOMBRES.get(idioma, idioma)
        if not ruta.exists():
            lineas.append(f"❌ {nombre}: no terminó")
            continue
        t = ruta.read_text("utf-8")
        dudosos = dato(t, r"Dudosos[^:]*: (\d+)") or "?"
        desplaz = dato(t, r"desplazados[^:]*: (\d+)") or "?"
        azure = dato(t, r"enviados a Azure en esta ejecución: ([\d.,]+)") or "0"
        usd = float((dato(t, r"API de Claude: ([\d.]+) USD") or "0"))
        total_usd += usd
        hechos.append(idioma)
        extra = f" · Azure {azure} car." if azure not in ("0",) else ""
        lineas.append(f"✅ {nombre}: {dudosos} dudosos · {desplaz} desplazados · USD {usd:.2f}{extra}")
    return lineas, total_usd, hechos


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--etapa", choices=["principal", "final"], required=True)
    p.add_argument("--resultado", required=True, help="success / failure / cancelled")
    p.add_argument("--carpeta", default="reportes")
    p.add_argument("--idiomas", default="")
    p.add_argument("--mezcla", default="", help="resultado de la mezcla dentro del doblaje (success/skipped/failure)")
    args = p.parse_args()

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("Sin NTFY_TOPIC: no se manda aviso.")
        return
    url_run = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}" \
              f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    ok = args.resultado == "success"
    carpeta = Path(args.carpeta)

    if args.etapa == "principal":
        pedidos = [x.strip() for x in args.idiomas.split(",") if x.strip()]
        lineas, usd, hechos = resumen_idiomas(carpeta, pedidos)
        titulo = (f"{'✅' if ok and len(hechos) == len(pedidos) else '⚠️'} {args.video}: proceso "
                  f"{len(hechos)}/{len(pedidos)} idiomas · USD {usd:.2f}")
        if not pedidos:
            titulo, lineas = f"❌ {args.video}: el proceso falló al preparar", ["Whisper o el guion fallaron. Abre la corrida."]
        if args.mezcla == "success":
            lineas.append("🎧 Mezcla lista: nube.sh bajar " + args.video)
        elif hechos:
            lineas.append(f"Falta el fondo: nube.sh fondo {args.video} RUTA/fondo.wav")
    else:
        mp3 = sorted(carpeta.glob("*.mp3"))
        titulo = f"{'🎧' if ok else '❌'} {args.video}: mezcla {'lista' if ok else 'detenida'}"
        lineas = ([f"{len(mp3)} MP3 listos. Bájalos: nube.sh bajar {args.video}"] if ok else
                  ["Se detuvo (revisa si el fondo dura distinto que la locución). Abre la corrida."])

    r = requests.post("https://ntfy.sh/", json={"topic": topic, "title": titulo, "message": "\n".join(lineas),
                                               "click": url_run, "priority": 3 if ok else 4}, timeout=30)
    print(titulo, "\n".join(lineas), sep="\n")
    print("ntfy:", r.status_code)


if __name__ == "__main__":
    sys.exit(main())
