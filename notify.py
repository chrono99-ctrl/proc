#!/usr/bin/env python3
"""notify.py — Manda un aviso al teléfono (app ntfy) con el resumen de una corrida.

Uso (en GitHub Actions):  python notify.py --video video07 --etapa principal --resultado success --carpeta reportes
Lee los reporte_XX.md de la carpeta. Si no hay NTFY_TOPIC (secreto del repo), no hace nada.
"""
import argparse
import base64
import os
import re
import sys
from pathlib import Path


LIMITE_NTFY = 4000        # bytes que acepta ntfy en un mensaje (el tope real es 4096)
MINUTOS_POR_IDIOMA = 3    # minutos que se listan por idioma; el resto va como "+N"


def dato(texto, patron):
    m = re.search(patron, texto)
    return m.group(1) if m else None


def seccion(texto, titulo):
    """Texto de la sección '## titulo...' del reporte ('' si no existe)."""
    m = re.search(rf"^## {titulo}[^\n]*\n(.*?)(?=^## |\Z)", texto, re.S | re.M)
    return m.group(1) if m else ""


def m_ss(mmss):
    """'05:28' -> '5:28'"""
    m, s = mmss.split(":")
    return f"{int(m)}:{s}"


def leer_reporte(texto):
    """Cantidades y minutos de un reporte_XX.md ya generado (no se recalcula nada). None si no se entiende."""
    cuentas = [dato(texto, p) for p in (r"Dudosos[^:]*: (\d+)", r"desplazados[^:]*: (\d+)",
                                       r"Silencios nuevos[^:]*: (\d+)")]
    if None in cuentas:
        return None
    dud, des, sil = (int(c) for c in cuentas)
    return {
        "dudosos": (dud, [m_ss(x) for x in re.findall(r"^### (\d+:\d{2}) \(id", seccion(texto, "Bloques a revisar"), re.M)]),
        "desfases": (des, [m_ss(x) for x in re.findall(r"^- (\d+:\d{2}) \(id", seccion(texto, "Bloques desplazados"), re.M)]),
        "silencios": (sil, [m_ss(x) for x in re.findall(r"^- (\d+:\d{2}) a ", seccion(texto, "Silencios para escuchar"), re.M)]),
    }


ETIQUETAS = {"dudosos": ("dudoso", "dudosos"), "desfases": ("desfase", "desfases"),
             "silencios": ("silencio", "silencios")}


def resumen_idioma(datos, con_minutos=True, cupo=MINUTOS_POR_IDIOMA):
    """'2 dudosos (4:10, 11:30) + 1 desfase (7:02)' o 'OK'. cupo=None: todos los minutos."""
    partes = []
    for clave in ("dudosos", "desfases", "silencios"):
        n, minutos = datos[clave]
        if n == 0:
            continue
        uno, varios = ETIQUETAS[clave]
        txt = f"{n} {uno if n == 1 else varios}"
        if con_minutos and minutos:
            mostrar = minutos if cupo is None else minutos[:max(cupo, 0)]
            if cupo is not None:
                cupo -= len(mostrar)
            resto = n - len(mostrar)
            detalle = ", ".join(mostrar) + (f" +{resto}" if resto > 0 else "") if mostrar else f"+{resto}"
            txt += f" ({detalle})"
        partes.append(txt)
    return " + ".join(partes) if partes else "OK"


def resumen_idiomas(carpeta, pedidos):
    """Devuelve (cortos, completos, usd, hechos): por idioma, el texto con minutos (máx. 3) y el detalle sin tope."""
    cortos, completos, total_usd, hechos = [], [], 0.0, []
    for idioma in pedidos:
        ruta = carpeta / f"reporte_{idioma}.md"
        if not ruta.exists():
            cortos.append(f"{idioma}: no terminó")
            completos.append(f"{idioma}: no terminó")
            continue
        t = ruta.read_text("utf-8")
        total_usd += float(dato(t, r"API de Claude: ([\d.]+) USD") or 0)
        datos = leer_reporte(t)
        hechos.append(idioma)
        if datos is None:
            cortos.append(f"{idioma}: reporte ilegible")
            completos.append(f"{idioma}: reporte ilegible")
            continue
        cortos.append(f"{idioma}: {resumen_idioma(datos)}")
        completos.append(f"{idioma}: {resumen_idioma(datos, cupo=None)}")
    return cortos, completos, total_usd, hechos


def sin_minutos(linea):
    return re.sub(r" \([^)]*\)", "", linea)


def codificar(texto):
    """Cabeceras HTTP con tildes o emojis (RFC 2047, lo entiende ntfy)."""
    return "=?UTF-8?B?" + base64.b64encode(texto.encode("utf-8")).decode() + "?="


def armar_aviso(carpeta, pedidos, limite=LIMITE_NTFY):
    """(resumen, adjunto, usd, hechos). Si el resumen con minutos no cabe, va el corto y el detalle como adjunto."""
    cortos, completos, usd, hechos = resumen_idiomas(carpeta, pedidos)
    resumen = " · ".join(cortos)
    adjunto = None
    if len(resumen.encode("utf-8")) > limite:
        adjunto = "\n".join(completos)
        tope = limite - min(200, limite // 4)                   # deja sitio a las líneas finales
        partes = [sin_minutos(c) for c in cortos]
        while partes and len(" · ".join(partes + ["…"]).encode("utf-8")) > tope:
            partes.pop()                                        # siempre termina: la lista se acorta
        resumen = " · ".join(partes + ["…"])
    return resumen, adjunto, usd, hechos


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--etapa", choices=["principal", "final"], required=True)
    p.add_argument("--resultado", required=True, help="success / failure / cancelled")
    p.add_argument("--carpeta", default="reportes")
    p.add_argument("--idiomas", default="")
    p.add_argument("--mezcla", default="", help="resultado de la mezcla dentro del doblaje (success/skipped/failure)")
    p.add_argument("--simular", action="store_true", help="muestra el aviso sin mandarlo")
    args = p.parse_args()

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    url_run = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}" \
              f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    ok = args.resultado == "success"
    carpeta = Path(args.carpeta)
    adjunto = None

    if args.etapa == "principal":
        pedidos = [x.strip() for x in args.idiomas.split(",") if x.strip()]
        resumen, adjunto, usd, hechos = armar_aviso(carpeta, pedidos)
        lineas = [resumen]
        titulo = (f"{'✅' if ok and len(hechos) == len(pedidos) else '⚠️'} {args.video}: proceso "
                  f"{len(hechos)}/{len(pedidos)} idiomas · USD {usd:.2f}")
        if not pedidos:
            titulo, lineas = f"❌ {args.video}: el proceso falló al preparar", ["Whisper o el guion fallaron. Abre la corrida."]
        if args.mezcla == "success":
            lineas.append("🎧 Mezcla lista: nube.sh bajar " + args.video)
        elif hechos:
            lineas.append(f"Falta el fondo: nube.sh fondo {args.video} RUTA/fondo.wav")
        if adjunto:
            lineas.append("(detalle completo adjunto)")
    else:
        mp3 = sorted(carpeta.glob("*.mp3"))
        titulo = f"{'🎧' if ok else '❌'} {args.video}: mezcla {'lista' if ok else 'detenida'}"
        lineas = ([f"{len(mp3)} MP3 listos. Bájalos: nube.sh bajar {args.video}"] if ok else
                  ["Se detuvo (revisa si el fondo dura distinto que la locución). Abre la corrida."])

    mensaje = "\n".join(lineas)
    print(titulo, mensaje, sep="\n")
    if adjunto:
        print("[adjunto detalle.txt]", adjunto, sep="\n")
    if args.simular or not topic:
        print("Simulación: no se manda aviso." if args.simular else "Sin NTFY_TOPIC: no se manda aviso.")
        return
    print("ntfy:", enviar(topic, titulo, mensaje, url_run, 3 if ok else 4, adjunto))


def enviar(topic, titulo, mensaje, url_run, prioridad, adjunto=None):
    """Manda el aviso; con adjunto, el resumen va en el mensaje y el detalle como archivo."""
    import requests
    if adjunto:
        try:
            r = requests.put(f"https://ntfy.sh/{topic}", data=adjunto.encode("utf-8"), timeout=30, headers={
                "Filename": "detalle.txt", "Title": codificar(titulo), "Message": codificar(mensaje),
                "Click": url_run, "Priority": str(prioridad)})
            if r.status_code < 300:
                return r.status_code
        except requests.RequestException:
            pass
        print("No se pudo adjuntar el detalle: va solo el resumen.")
    r = requests.post("https://ntfy.sh/", json={"topic": topic, "title": titulo, "message": mensaje,
                                               "click": url_run, "priority": prioridad}, timeout=30)
    return r.status_code


if __name__ == "__main__":
    sys.exit(main())
