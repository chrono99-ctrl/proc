#!/usr/bin/env python3
"""
hacer_srt.py — Subtitulos .srt para los videos doblados.

Usa lo que ya dejo doblar.py / doblar_v2.py en <salida>/_trabajo:
  bloques_es.json          -> bloques en espanol con sus tiempos
  <idioma>/estado.json     -> texto final que leyo la voz de cada bloque
  cache_voz/*.wav          -> audio de cada bloque (para medir cuanto dura)

No gasta Azure ni tu plan Pro: solo lee archivos y escribe los .srt.

Uso (dentro de ~/doblaje, con el entorno activado):
  python hacer_srt.py --salida video02

Resultado: <salida>/subtitulos/<salida>_es_espanol.srt, _de_aleman.srt ...
(ya salen con el nombre del idioma: no hay que renombrarlos)
Los idiomas que aun no terminaron de doblarse se saltan.

Version 2 (8 nov 2026): sin palabras sueltas ni subtitulos muy cortos.
 - Las frases largas se reparten en partes parejas (no "lleno + cola").
 - Un subtitulo de menos de 3 palabras o menos de 1,2 s se une al vecino;
   si juntos quedan muy largos, se reparten en dos mitades parejas.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import soundfile as sf

SR = 48000
SIN_ESPACIOS = {"ja", "zh", "th"}
MAX_LINEA = 42          # caracteres por linea (alfabetos con espacios)
MAX_LINEA_CJK = 20      # caracteres por linea (japones, chino, tailandes)
MAX_DURACION = 6.5      # segundos maximos por subtitulo
MIN_DURACION = 1.2      # menos que esto se une al vecino
MIN_PALABRAS = 3        # menos palabras que esto se une al vecino
MIN_CJK = 6             # en ja/zh/th: menos caracteres que esto se une
PUNTUACION = ",;:،、，"

NOMBRES = {"es": "espanol", "de": "aleman", "fr": "frances", "ja": "japones", "ru": "ruso",
           "it": "italiano", "en": "ingles", "pt": "portugues", "ar": "arabe", "id": "indonesio",
           "vi": "vietnamita", "ko": "coreano", "tr": "turco", "th": "tailandes", "pl": "polaco",
           "zh": "chino", "hi": "hindi", "nl": "neerlandes", "sv": "sueco", "uk": "ucraniano"}


def leer_json(ruta):
    return json.loads(ruta.read_text("utf-8"))


def tiempo(seg):
    ms = int(round(seg * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def cortar_cerca(t, meta):
    """Corta t en dos lo mas cerca posible del caracter 'meta', prefiriendo despues de una coma."""
    if " " in t:
        cands = [i for i, c in enumerate(t) if c == " "]
        i = min(cands, key=lambda k: abs(k - meta) - (6 if k > 0 and t[k - 1] in PUNTUACION else 0))
        return t[:i].strip(), t[i + 1:].strip()
    if len(t) < 2:
        return t, ""
    i = min(range(1, len(t)), key=lambda k: abs(k - meta) - (12 if t[k - 1] in PUNTUACION else 0))
    return t[:i], t[i:]


def unir(a, b):
    sep = "" if re.search(r"[　-鿿฀-๿]$", a) else " "
    return a + sep + b


def repartir(f, max_total):
    """Reparte una frase larga en partes de largo parecido (nunca deja una cola de una palabra)."""
    n = -(-len(f) // max_total)
    partes, resto = [], f
    for quedan in range(n - 1, 0, -1):
        a, b = cortar_cerca(resto, len(resto) / (quedan + 1))
        if not a or not b:
            break
        partes.append(a)
        resto = b
    partes.append(resto)
    return partes


def trozos(texto, max_total):
    """Parte un bloque en subtitulos de hasta ~max_total caracteres."""
    texto = " ".join(texto.split())
    frases = [f.strip() for f in re.split(r"(?<=[.!?…。！？؟])\s*", texto) if f.strip()]
    salida = []
    for f in frases:
        salida += repartir(f, max_total)
    # une frases muy cortas con la vecina
    unidas = []
    for f in salida:
        if unidas and len(unidas[-1]) + 1 + len(f) <= max_total and \
                (len(unidas[-1]) < max_total // 2 or len(f) < max_total // 2):
            unidas[-1] = unir(unidas[-1], f)
        else:
            unidas.append(f)
    return unidas


def dos_lineas(texto, max_linea):
    if len(texto) <= max_linea:
        return texto
    a, b = cortar_cerca(texto, len(texto) / 2)
    return f"{a}\n{b}" if a and b else texto


def partir_en_subtitulos(texto, inicio, fin, idioma):
    cjk = idioma in SIN_ESPACIOS
    max_linea = MAX_LINEA_CJK if cjk else MAX_LINEA
    max_total = max_linea * 2
    partes = trozos(texto, max_total)
    dur = max(fin - inicio, 0.5)
    total = sum(len(p) for p in partes) or 1

    def segundos(p):
        return dur * len(p) / total

    # 1) si algun subtitulo duraria mas de MAX_DURACION, se parte en dos mitades parejas
    i = 0
    while i < len(partes):
        p = partes[i]
        if segundos(p) > MAX_DURACION and len(p) > 24:
            a, b = cortar_cerca(p, len(p) / 2)
            if a and b:
                partes[i:i + 1] = [a, b]
                continue
        i += 1

    # 2) ningun subtitulo de una o dos palabras ni de menos de 1,2 s
    def corto(p):
        if segundos(p) < MIN_DURACION:
            return True
        return len(p) < MIN_CJK if cjk else len(p.split()) < MIN_PALABRAS

    i = 0
    while i < len(partes) and len(partes) > 1:
        if not corto(partes[i]):
            i += 1
            continue
        j = i - 1 if i > 0 else i
        juntos = unir(partes[j], partes[j + 1])
        if len(juntos) <= max_total + 10:
            partes[j:j + 2] = [juntos]
            i = j
        else:
            a, b = cortar_cerca(juntos, len(juntos) / 2)
            partes[j:j + 2] = [a, b]
            i = j + 2

    res, t = [], inicio
    for p in partes:
        d = segundos(p)
        res.append((t, min(t + d, fin), dos_lineas(p, max_linea)))
        t += d
    return res


def escribir(ruta, subs):
    lineas = []
    for n, (a, b, txt) in enumerate(subs, 1):
        if b - a < 0.4:
            b = a + 0.4
        lineas += [str(n), f"{tiempo(a)} --> {tiempo(b)}", txt, ""]
    ruta.write_text("\n".join(lineas), "utf-8")


def srt_espanol(bloques):
    subs = []
    for i, b in enumerate(bloques):
        fin = b["fin"]
        if i + 1 < len(bloques):
            fin = min(fin, bloques[i + 1]["inicio"] - 0.05)
        subs += partir_en_subtitulos(b["texto"], b["inicio"], max(fin, b["inicio"] + 0.5), "es")
    return subs


def srt_doblado(bloques, estado, cache, idioma):
    """Replica la colocacion de doblar.py: cada bloque empieza en su minuto o apenas despues
    del anterior, y dura lo que dura su audio."""
    subs, cursor = [], 0.0
    for b in bloques:
        reg = estado[str(b["id"])]
        wav = cache / reg["wav"]
        dur = sf.info(str(wav)).duration
        inicio = max(b["inicio"], cursor + 0.1)
        subs += partir_en_subtitulos(reg["texto"], inicio, inicio + dur, idioma)
        cursor = inicio + dur
    return subs


def nombre_archivo(video, idioma):
    return f"{video}_{idioma}_{NOMBRES[idioma]}.srt" if idioma in NOMBRES else f"{video}_{idioma}.srt"


def main():
    p = argparse.ArgumentParser(description="Crea los .srt de un video ya doblado")
    p.add_argument("--salida", required=True, help="carpeta del video (ej.: video02)")
    args = p.parse_args()

    carpeta = Path(args.salida)
    trabajo = carpeta / "_trabajo"
    if not (trabajo / "bloques_es.json").exists():
        sys.exit(f"No encuentro {trabajo}/bloques_es.json. Ejecuta el comando desde ~/doblaje "
                 f"y revisa el nombre de la carpeta (--salida {args.salida}).")
    bloques = leer_json(trabajo / "bloques_es.json")
    cache = trabajo / "cache_voz"
    destino = carpeta / "subtitulos"
    destino.mkdir(exist_ok=True)
    nombre = carpeta.name

    escribir(destino / nombre_archivo(nombre, "es"), srt_espanol(bloques))
    print(f"OK  es  ({len(bloques)} bloques)")

    hechos, saltados = 1, []
    for d in sorted(x for x in trabajo.iterdir() if x.is_dir() and x.name != "cache_voz"):
        ruta_estado = d / "estado.json"
        if not ruta_estado.exists():
            continue
        estado = leer_json(ruta_estado)
        faltan = [b["id"] for b in bloques if str(b["id"]) not in estado]
        if faltan:
            saltados.append(d.name)
            print(f"--  {d.name}  incompleto (faltan {len(faltan)} bloques), se salta")
            continue
        try:
            subs = srt_doblado(bloques, estado, cache, d.name)
        except (KeyError, FileNotFoundError) as e:
            print(f"!!  {d.name}  no se pudo leer ({e!r}); se salta")
            saltados.append(d.name)
            continue
        escribir(destino / nombre_archivo(nombre, d.name), subs)
        print(f"OK  {d.name}  ({len(subs)} subtitulos)")
        hechos += 1

    print(f"\n{hechos} archivos .srt en {destino}")
    if saltados:
        print("Saltados:", ", ".join(saltados))
    print("En Studio: panel del idioma -> Subtitulos manuales -> Agregar -> Subir archivo -> 'Con sincronizacion'.")


if __name__ == "__main__":
    main()
