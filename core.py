#!/usr/bin/env python3
"""
doblaje.py — Doblaje multi-idioma de narraciones en la nube (GitHub Actions).

Tres pasos (cada uno es un comando):
  1) preparar : une los capítulos de ElevenLabs (PREFIJO-capNN.wav, en orden y sin espacio), saca los TIEMPOS con
                Whisper y toma el TEXTO del guion (PREFIJO-capNN.txt, sin las etiquetas <break>).
                python doblaje.py preparar --capitulos entrada/ --salida trabajo/
  2) idioma   : traduce (Sonnet), ajusta duración (Haiku), genera la voz (edge-tts + filtro; Azure de respaldo),
                revisa con Whisper. Deja la PISTA DE VOZ sola (sin fondo), el reporte y el .srt del idioma.
                python doblaje.py idioma --idioma de --salida trabajo/ --video video07
  3) mezclar  : cuando existe fondo.wav (exportado de DaVinci): compara duraciones, mezcla y exporta los MP3.
                python doblaje.py mezclar --fondo fondo.wav --salida trabajo/ --video video07

Cómo trabaja:
  - El texto sale del guion, no de lo que entendió Whisper (Whisper solo da los tiempos).
  - El doblaje parte desde la locución, sin esperar a DaVinci: solo la mezcla espera el fondo.
  - Antes de mezclar se compara la duración de la voz con la del fondo: si el fondo solo es más largo al final
    (cola de cierre, hasta COLA_MAX s), se rellena con silencio; si no cuadra, se detiene y avisa.
Retomable: el estado de cada idioma (traducción, bloques hechos, audios) se guarda y no se vuelve a gastar.
"""
import argparse
import asyncio
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np
import requests
import soundfile as sf
from dotenv import load_dotenv

SR = 48000
# whisper.cpp ya instalado en el Mac (no mover esa carpeta)
WHISPER_DIR = Path(os.path.expanduser(os.environ.get("WHISPER_DIR", "~/Movies/Animaciones/_compartido/whisper")))
WHISPER_CLI = WHISPER_DIR / "build" / "bin" / "whisper-cli"
WHISPER_MODELO = Path(os.path.expanduser(os.environ.get("WHISPER_MODELO", str(WHISPER_DIR / "models" / "ggml-large-v3-turbo.bin"))))
WHISPER_HILOS = str(os.cpu_count() or 4)
USO_DIR = Path(os.path.expanduser(os.environ.get("USO_DIR", str(Path(__file__).parent))))   # contadores de gasto
COLA_MAX = float(os.environ.get("COLA_MAX", "15"))   # segundos que el fondo puede durar de más SOLO al final
MODELO_CLAUDE = os.environ.get("MODELO_CLAUDE", "sonnet")   # Pro: traducción (alias de Claude Code)
MODELO_CLAUDE_AJUSTE = os.environ.get("MODELO_CLAUDE_AJUSTE", "haiku")   # Pro: reescrituras y revisión
MOTOR = os.environ.get("MOTOR", "pro")   # "api" = API de Claude (crédito) · "pro" = claude -p (suscripción Pro)
MODELO_TRADUCCION = os.environ.get("MODELO_TRADUCCION", "claude-sonnet-5-5")   # traducción: calidad
MODELO_AJUSTE = os.environ.get("MODELO_AJUSTE", "claude-haiku-5-5")            # reescrituras y revisión: volumen
TOPE_CLAUDE_USD = float(os.environ.get("TOPE_CLAUDE_USD", "4.5"))   # gasto acumulado máximo en la API
RUTA_USO_CLAUDE = USO_DIR / "uso_claude.json"
PRECIOS = {   # USD por millón de tokens (entrada, salida). Verificado el 10 nov 2026.
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),    # prompts de menos de 100.000 tokens (los nuestros son mucho menores)
    "claude-opus-5-5": (4.0, 20.0),
}
# v3: la voz va SIEMPRE a velocidad normal. Si no cabe o queda corta, se reescribe el texto.
VEL_EMERGENCIA = 10      # % solo si tras 3 reescrituras aún no cabe (queda anotado en el reporte; 0 = nunca)
HUECO_ALARGAR = 2.0      # si la voz dura esto menos que el español, se reescribe más larga (v3: 1,5)
HUECO_REPORTE = 3.0      # silencios nuevos que se listan en el reporte
UMBRAL_ERROR = 0.25      # diferencia texto vs. lo que entiende Whisper que dispara revisión (v3: 0,20)
MARGEN = 0.15            # segundos de respiro entre bloques
BLOQUE_IDEAL = 12        # a partir de aquí se cierra el bloque al terminar una frase
BLOQUE_MAX = 25          # un bloque no pasa de esto (salvo una frase sin comas ni pausas)
PAUSA_CORTE = 0.6        # una pausa así al final de una frase cierra el bloque
VOZ_MOTOR = os.environ.get("VOZ_MOTOR", "edge")   # v5: "edge" = edge-tts gratis (Azure de respaldo) · "azure" = solo Azure
_LIMITE_DEF = "5000000" if time.strftime("%Y-%m-%d") < "2026-12-07" else "500000"
LIMITE_MENSUAL = int(os.environ.get("LIMITE_AZURE", _LIMITE_DEF))
RUTA_USO = USO_DIR / "uso_azure.json"
FIN_FRASE = ".?!…"

# Voces masculinas por defecto (cámbialas aquí si prefieres otras).
VOCES = {
    "en": "en-US-AndrewNeural",   "pt": "pt-BR-AntonioNeural",  "ar": "ar-SA-HamedNeural",
    "de": "de-DE-ConradNeural",   "fr": "fr-FR-HenriNeural",    "it": "it-IT-DiegoNeural",
    "ja": "ja-JP-KeitaNeural",    "ko": "ko-KR-InJoonNeural",   "zh": "zh-CN-YunxiNeural",
    "ru": "ru-RU-DmitryNeural",   "uk": "uk-UA-OstapNeural",    "pl": "pl-PL-MarekNeural",
    "tr": "tr-TR-AhmetNeural",    "nl": "nl-NL-MaartenNeural",  "sv": "sv-SE-MattiasNeural",
    "hi": "hi-IN-MadhurNeural",   "id": "id-ID-ArdiNeural",     "vi": "vi-VN-NamMinhNeural",
    "th": "th-TH-NiwatNeural",    "ro": "ro-RO-EmilNeural",     "cs": "cs-CZ-AntoninNeural",
    "el": "el-GR-NestorasNeural", "hu": "hu-HU-TamasNeural",    "he": "he-IL-AvriNeural",
}
NOMBRES = {
    "en": "inglés", "pt": "portugués de Brasil", "ar": "árabe", "de": "alemán", "fr": "francés",
    "it": "italiano", "ja": "japonés", "ko": "coreano", "zh": "chino mandarín", "ru": "ruso",
    "uk": "ucraniano", "pl": "polaco", "tr": "turco", "nl": "neerlandés", "sv": "sueco",
    "hi": "hindi", "id": "indonesio", "vi": "vietnamita", "th": "tailandés", "ro": "rumano",
    "cs": "checo", "el": "griego", "hu": "húngaro", "he": "hebreo",
}
SIN_ESPACIOS = {"ja", "zh", "th"}  # se mide por caracteres, no por palabras


# ---------------------------------------------------------------- utilidades

def log(msg):
    print(msg, flush=True)


def ffmpeg(*args):
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"Error de ffmpeg: {r.stderr.strip()}")


def a_wav(origen, destino, canales):
    if not destino.exists():
        ffmpeg("-i", str(origen), "-ac", str(canales), "-ar", str(SR), "-c:a", "pcm_s16le", str(destino))
    return destino


def leer_json(p, defecto=None):
    return json.loads(p.read_text("utf-8")) if p.exists() else defecto


def guardar_json(p, datos):
    p.write_text(json.dumps(datos, ensure_ascii=False, indent=2), "utf-8")


def sin_tildes(t):
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def nombre_mp3(video, idioma):
    """video02_de_aleman.mp3 (mismo formato que los SRT de hacer_srt.py)."""
    nombre = sin_tildes(NOMBRES.get(idioma, idioma).split()[0]).lower()
    return f"{video}_{idioma}_{nombre}.mp3"


def normalizar(t):
    t = unicodedata.normalize("NFKC", t).lower()
    t = "".join(" " if unicodedata.category(c)[0] in "PS" else c for c in t)
    return " ".join(t.split())


def diferencia(ref, hyp, idioma):
    import jiwer
    ref, hyp = normalizar(ref), normalizar(hyp)
    if not hyp:
        return 1.0
    if idioma in SIN_ESPACIOS or idioma in {"ko"}:
        return jiwer.cer(ref.replace(" ", ""), hyp.replace(" ", ""))
    return jiwer.wer(ref, hyp)


def extraer_json(texto):
    m = re.search(r"(\[.*\]|\{.*\})", texto, re.S)
    if not m:
        raise ValueError("Claude no devolvió JSON")
    return json.loads(m.group(1))


def mmss(seg):
    m, s = divmod(int(seg), 60)
    return f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------- Whisper (whisper.cpp)

def transcribir(ruta, idioma, con_palabras=False):
    """Transcribe con whisper-cli. Devuelve {"text": ..., "segments": [{start, end, text, tokens}]}.
    Con con_palabras=True pide el JSON completo (-ojf), que trae el tiempo de cada token."""
    if not WHISPER_CLI.exists() or not WHISPER_MODELO.exists():
        sys.exit(f"No encuentro whisper-cli ({WHISPER_CLI}) o el modelo ({WHISPER_MODELO})")
    with tempfile.TemporaryDirectory() as tmp:
        wav16 = Path(tmp) / "audio16k.wav"
        # whisper.cpp necesita WAV a 16 kHz, mono
        ffmpeg("-i", str(ruta), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav16))
        base = Path(tmp) / "salida"
        cmd = [str(WHISPER_CLI), "-m", str(WHISPER_MODELO), "-f", str(wav16),
               "-l", idioma, "-mc", "0", "-t", WHISPER_HILOS, "-oj", "-of", str(base), "-np"]
        if con_palabras:
            cmd.insert(cmd.index("-oj") + 1, "-ojf")
        r = subprocess.run(cmd, capture_output=True, text=True)
        js = base.with_suffix(".json")
        if r.returncode != 0 or not js.exists():
            sys.exit(f"Error de whisper-cli: {r.stderr.strip()[-500:]}")
        datos = json.loads(js.read_text("utf-8", errors="replace"))
    segmentos = []
    for s in datos.get("transcription", []):
        texto = s.get("text", "").strip()
        if not texto:
            continue
        tokens = []
        for t in s.get("tokens", []) or []:
            tt = t.get("text", "")
            off = t.get("offsets") or {}
            if tt.startswith("[_") or tt.startswith("<|") or "to" not in off:
                continue
            tokens.append({"text": tt, "fin": off["to"] / 1000})
        segmentos.append({"start": s["offsets"]["from"] / 1000, "end": s["offsets"]["to"] / 1000,
                          "text": texto, "tokens": tokens})
    return {"text": " ".join(s["text"] for s in segmentos), "segments": segmentos}


# ---------------------------------------------------------------- bloques por frases (v3)

def cierra(texto):
    return texto.rstrip().rstrip("\"'»”)").rstrip()[-1:] in FIN_FRASE


def piezas_de_segmento(s):
    """Parte un segmento de Whisper en trozos que terminan en fin de frase, cada uno con su tiempo.
    El tiempo de cada fin de frase sale del token con el punto; si no cuadra, se reparte por letras."""
    partes = [p for p in re.split(r"(?<=[.?!…])\s+", s["text"].strip()) if p]
    ini, fin = s["start"], s["end"]
    if len(partes) == 1:
        return [{"inicio": ini, "fin": fin, "texto": partes[0], "cierra": cierra(partes[0])}]

    # tiempos de los tokens que cierran frase (un punto seguido de espacio o al final, no "1.200")
    toks = s.get("tokens", [])
    marcas = []
    for i, t in enumerate(toks):
        if t["text"].rstrip()[-1:] in FIN_FRASE:
            sig = toks[i + 1]["text"] if i + 1 < len(toks) else " "
            if sig[:1].isspace() or i + 1 == len(toks):
                marcas.append(min(max(t["fin"], ini), fin))
    necesarias = len(partes) - 1
    if len(marcas) >= necesarias and all(b >= a for a, b in zip(marcas, marcas[1:])):
        cortes = marcas[:necesarias]
    else:   # sin tokens fiables: repartir el tiempo del segmento según las letras
        total = sum(len(p) for p in partes) or 1
        cortes, acum = [], 0
        for p in partes[:-1]:
            acum += len(p)
            cortes.append(ini + (fin - ini) * acum / total)
    piezas, t0 = [], ini
    for p, t1 in zip(partes, cortes + [fin]):
        piezas.append({"inicio": t0, "fin": t1, "texto": p, "cierra": cierra(p)})
        t0 = t1
    return piezas


def partir_frase_larga(piezas):
    """Si una frase pasa de BLOQUE_MAX, la parte entre dos trozos: primero en una coma, si no en la pausa
    más larga. Nunca en una palabra a media frase (los trozos son segmentos completos de Whisper)."""
    dur = piezas[-1]["fin"] - piezas[0]["inicio"]
    if dur <= BLOQUE_MAX or len(piezas) < 2:
        return [piezas]
    mejor, puntaje_mejor = None, None
    for j in range(len(piezas) - 1):
        izq = piezas[j]["fin"] - piezas[0]["inicio"]
        der = piezas[-1]["fin"] - piezas[j + 1]["inicio"]
        if min(izq, der) < 3:
            continue
        coma = piezas[j]["texto"].rstrip()[-1:] in ",;:—–"
        pausa = piezas[j + 1]["inicio"] - piezas[j]["fin"]
        equilibrio = -abs(izq - der) / dur          # preferir cerca de la mitad
        puntaje = (2 if coma else 0) + min(pausa, 1.5) + equilibrio
        if puntaje_mejor is None or puntaje > puntaje_mejor:
            mejor, puntaje_mejor = j, puntaje
    if mejor is None:
        return [piezas]
    return partir_frase_larga(piezas[:mejor + 1]) + partir_frase_larga(piezas[mejor + 1:])


def armar_bloques(segmentos):
    """v3: agrupa frases completas en bloques. Solo se corta al final de una frase
    (o, en una frase de más de 25 s, en una coma o pausa)."""
    return bloques_desde_piezas([p for s in segmentos if s["text"].strip() for p in piezas_de_segmento(s)])


def bloques_desde_piezas(piezas):
    """Piezas = trozos con {inicio, fin, texto, cierra}, en orden (de Whisper o de los tiempos de ElevenLabs)."""

    # 1) frases completas (lista de trozos), partiendo las demasiado largas
    frases, actual = [], []
    for p in piezas:
        actual.append(p)
        if p["cierra"]:
            frases += partir_frase_larga(actual)
            actual = []
    if actual:
        frases += partir_frase_larga(actual)

    # 2) bloques de una o varias frases
    bloques, b = [], None
    for f in frases:
        ini, fin = f[0]["inicio"], f[-1]["fin"]
        texto = " ".join(x["texto"].strip() for x in f)
        if b:
            pausa = ini - b["fin"]
            dur = b["fin"] - b["inicio"]
            if pausa >= PAUSA_CORTE or dur >= BLOQUE_IDEAL or fin - b["inicio"] > BLOQUE_MAX:
                bloques.append(b)
                b = None
        if b is None:
            b = {"inicio": ini, "fin": fin, "texto": texto}
        else:
            b["fin"] = fin
            b["texto"] += " " + texto
    if b:
        bloques.append(b)
    for i, x in enumerate(bloques):
        x["id"] = i
        x["inicio"] = round(x["inicio"], 2)
        x["fin"] = round(x["fin"], 2)
        x["v3"] = True
    return bloques


def quitar_alucinaciones(bloques, ruta_voz):
    """Whisper a veces "oye" frases en el silencio (típico: «Gracias.» al final). Si en el tramo de un bloque
    la voz en español está en silencio, ese bloque no existe: se quita."""
    voz, sr = sf.read(str(ruta_voz), dtype="float32")
    if voz.ndim > 1:
        voz = voz.mean(axis=1)
    buenos = []
    for b in bloques:
        tramo = voz[int(b["inicio"] * sr):int(b["fin"] * sr)]
        if len(tramo) and np.max(np.abs(tramo)) > 0.01:
            buenos.append(b)
        else:
            log(f"   se quita un bloque en silencio (Whisper lo inventó): {mmss(b['inicio'])}")
    for i, b in enumerate(buenos):
        b["id"] = i
    return buenos


# ---------------------------------------------------------------- nube: capítulos + guion (mejoras 19 nov)

RE_CAP = re.compile(r"^(?P<pre>.+)-cap(?P<n>\d+)\.(?P<ext>wav|txt)$", re.IGNORECASE)
RE_BREAK = re.compile(r"<break\b[^>]*/>")


def capitulos(carpeta):
    """[(n, wav, txt)] en orden. Ignora copias '_antes', 'descartado' y ocultos (igual que generar_locucion.py).
    Si un capítulo se corrigió, se sube el corregido con el nombre normal."""
    wavs, txts = {}, {}
    for f in Path(carpeta).iterdir():
        m = RE_CAP.match(f.name)
        if not m or f.name.startswith((".", "_")) or "antes" in f.name.lower() or "descartado" in f.name.lower():
            continue
        (wavs if m.group("ext").lower() == "wav" else txts)[int(m.group("n"))] = f
    if not wavs:
        sys.exit(f"No hay archivos PREFIJO-capNN.wav en {carpeta}")
    sin_txt = sorted(set(wavs) - set(txts))
    if sin_txt:
        sys.exit(f"Faltan los guiones (PREFIJO-capNN.txt) de los capítulos: {sin_txt}")
    return [(n, wavs[n], txts[n]) for n in sorted(wavs)]


def unir_capitulos(caps, destino):
    """Une los WAV de capítulo en orden y sin espacio entre ellos (como quedan en el video)."""
    if destino.exists():
        return destino
    with tempfile.TemporaryDirectory() as d:
        lista = Path(d) / "lista.txt"
        partes = []
        for n, wav, _ in caps:
            p = Path(d) / f"c{n:03d}.wav"
            ffmpeg("-i", str(wav), "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(p))
            partes.append(p)
        lista.write_text("".join(f"file '{p}'\n" for p in partes), "utf-8")
        ffmpeg("-f", "concat", "-safe", "0", "-i", str(lista), "-c", "copy", str(destino))
    return destino


def texto_guion(caps):
    """Texto hablado del guion: sin etiquetas <break> ni espacios de más."""
    return " ".join(re.sub(r"\s+", " ", RE_BREAK.sub(" ", txt.read_text("utf-8-sig"))).strip() for _, _, txt in caps)


def piezas_desde_elevenlabs(ruta_json, offset):
    """Mejora 20 nov: los tiempos salen del .json que guarda locucion.sh (/with-timestamps de ElevenLabs).
    Usa `alignment` (calza 1 a 1 con el texto enviado). Los caracteres de una <break> se ignoran; el fin de una
    frase es el fin de su signo de puntuación (el espacio siguiente se lleva el silencio). Devuelve piezas
    cortadas en fin de frase y en comas (para poder partir frases largas)."""
    datos = json.loads(Path(ruta_json).read_text("utf-8"))
    piezas = []
    for seg in datos["segmentos"]:
        al = seg["alignment"]
        chars, ini_c, fin_c = al["characters"], al["character_start_times_seconds"], al["character_end_times_seconds"]
        base = offset + float(seg.get("offset_s", 0))
        texto = "".join(chars)
        saltar = set()
        for m in RE_BREAK.finditer(texto):
            saltar.update(range(m.start(), m.end()))
        palabras, cur = [], None
        for i, c in enumerate(chars):
            if i in saltar or c.isspace():
                if cur:
                    palabras.append(cur)
                cur = None
                continue
            if cur is None:
                cur = {"t": c, "ini": ini_c[i], "fin": fin_c[i]}
            else:
                cur["t"] += c
                cur["fin"] = fin_c[i]
        if cur:
            palabras.append(cur)
        actual = []
        for k, w in enumerate(palabras):
            actual.append(w)
            fin_palabra = w["t"].rstrip("\"'»”)")[-1:]
            if fin_palabra in FIN_FRASE or fin_palabra in ",;:" or k == len(palabras) - 1:
                t = " ".join(x["t"] for x in actual)
                piezas.append({"inicio": base + actual[0]["ini"], "fin": base + actual[-1]["fin"],
                               "texto": t, "cierra": cierra(t)})
                actual = []
    return piezas


def json_valido(ruta_json, ruta_txt, ruta_wav):
    """El .json sirve si su texto es el del guion y su duración calza con el WAV (si no, ese capítulo va por Whisper)."""
    if not ruta_json.exists():
        return False, "sin .json"
    try:
        datos = json.loads(ruta_json.read_text("utf-8"))
        enviado = " ".join("".join(s["alignment"]["characters"]) for s in datos["segmentos"])
    except Exception as e:
        return False, f".json ilegible ({type(e).__name__})"
    limpio = lambda t: re.sub(r"\s+", " ", RE_BREAK.sub(" ", t)).strip()
    import difflib
    parecido = difflib.SequenceMatcher(None, limpio(enviado), limpio(ruta_txt.read_text("utf-8-sig")),
                                       autojunk=False).ratio()
    if parecido < 0.98:
        return False, f"el texto del .json no es el del guion ({parecido:.0%})"
    dj, dw = datos.get("duracion_wav_s"), duracion(ruta_wav)
    if dj is not None and abs(float(dj) - dw) > 0.25:
        return False, f"el .json dura {float(dj):.2f} s y el WAV {dw:.2f} s"
    return True, ""


def _norm_palabra(w):
    return sin_tildes(normalizar(w)).replace(" ", "")


def alinear_con_guion(bloques, guion):
    """Mejora 1: el texto de cada bloque sale del GUION; Whisper solo aporta los tiempos.
    Alinea palabra a palabra lo que entendió Whisper con el guion y reparte las palabras del guion en los bloques.
    Las palabras que Whisper no entendió igual (nombres, números) se asignan por contexto: si el tramo sin
    coincidencia cruza un límite de bloque, se corta en el último fin de frase del tramo."""
    import difflib
    w_norm, w_bloque = [], []
    for b in bloques:
        for w in b["texto"].split():
            n = _norm_palabra(w)
            if n:
                w_norm.append(n)
                w_bloque.append(b["id"])
    g_orig = guion.split()
    g_norm = [_norm_palabra(w) for w in g_orig]
    asign = [None] * len(g_orig)
    coinciden = 0
    sm = difflib.SequenceMatcher(None, w_norm, g_norm, autojunk=False)
    for a, g, n in sm.get_matching_blocks():
        for k in range(n):
            asign[g + k] = w_bloque[a + k]
        coinciden += n
    # tramos sin coincidencia
    i = 0
    while i < len(asign):
        if asign[i] is not None:
            i += 1
            continue
        j = i
        while j < len(asign) and asign[j] is None:
            j += 1
        antes = asign[i - 1] if i > 0 else None
        despues = asign[j] if j < len(asign) else None
        if antes is None or despues is None or antes == despues:
            relleno = [antes if antes is not None else despues] * (j - i)
        else:
            corte = None   # índice (dentro del tramo) de la última palabra que cierra frase
            for k in range(i, j):
                if cierra(g_orig[k]):
                    corte = k
            if corte is None:   # sin fin de frase: si la palabra previa cerraba frase, el tramo va al bloque siguiente
                corte = i - 1 if cierra(g_orig[i - 1]) else j - 1
            relleno = [antes if k <= corte else despues for k in range(i, j)]
        asign[i:j] = relleno
        i = j
    textos = {}
    for w, b in zip(g_orig, asign):
        textos.setdefault(b, []).append(w)
    for b in bloques:
        b["texto_whisper"] = b["texto"]
        b["texto"] = " ".join(textos.get(b["id"], [])) or b["texto"]
    return coinciden / max(len(g_norm), 1)


# ---------------------------------------------------------------- Claude (Claude Code, suscripción Pro)

class UsoClaude:
    """Cuenta lo gastado en la API de Claude (uso_claude.json, en USD) y frena al llegar al tope."""

    def __init__(self, ruta=RUTA_USO_CLAUDE, tope=TOPE_CLAUDE_USD):
        self.ruta, self.tope = ruta, tope
        self.datos = leer_json(ruta, {"total_usd": 0.0, "llamadas": 0, "por_modelo": {}})

    @property
    def total(self):
        return self.datos["total_usd"]

    def revisar(self):
        if self.total >= self.tope:
            sys.exit(f"Se alcanzó el tope de gasto en la API de Claude: {self.total:.2f} de {self.tope:.2f} USD.\n"
                     "Si te queda crédito (o recargaste), sube el tope y ejecuta el mismo comando; retoma donde quedó.\n"
                     "Ejemplo: TOPE_CLAUDE_USD=9 caffeinate -i python doblar_v4.py ...")

    def sumar(self, modelo, entrada, salida):
        clave = next((k for k in PRECIOS if modelo.startswith(k)), None)   # acepta sufijos de fecha
        p_in, p_out = PRECIOS[clave] if clave else (4.0, 20.0)   # modelo desconocido: se cuenta caro
        usd = entrada * p_in / 1e6 + salida * p_out / 1e6
        m = self.datos["por_modelo"].setdefault(modelo, {"usd": 0.0, "entrada": 0, "salida": 0, "llamadas": 0})
        m["usd"] += usd
        m["entrada"] += entrada
        m["salida"] += salida
        m["llamadas"] += 1
        self.datos["total_usd"] += usd
        self.datos["llamadas"] += 1
        guardar_json(self.ruta, self.datos)


class Claude:
    """El "cerebro" del doblaje. v4: API de Claude por defecto; MOTOR=pro usa claude -p como la v3."""

    def __init__(self):
        self.motor = MOTOR
        self.uso = UsoClaude()
        # Con el Pro no se paga, pero se ANOTA lo que habría costado en la API (tokens reales × precios)
        self.estimado = UsoClaude(ruta=RUTA_USO_CLAUDE.with_name("uso_claude_estimado_pro.json"), tope=float("inf"))
        self.medidor = self.uso if self.motor == "api" else self.estimado
        if self.motor == "api":
            try:
                import anthropic
            except ImportError:
                sys.exit("Falta el paquete anthropic. Ejecuta: pip install anthropic (con el venv activado)")
            clave = os.environ.get("ANTHROPIC_API_KEY")
            if not clave:
                sys.exit("Falta ANTHROPIC_API_KEY en el archivo .env")
            self.cliente = anthropic.Anthropic(api_key=clave, max_retries=0, timeout=600)
        else:
            if not shutil.which("claude"):
                sys.exit("No encuentro Claude Code ('claude') en la Terminal.")
            # Sin ANTHROPIC_API_KEY: así Claude Code usa tu suscripción Pro y no cobra como API.
            self.env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
            self.carpeta = tempfile.mkdtemp(prefix="doblaje_claude_")  # carpeta vacía: no toca tus archivos
        self.modelos_vistos = set()

    def pedir(self, sistema, usuario, max_tokens=None, tarea="ajuste"):
        if self.motor != "api":
            return self._pedir_pro(sistema, usuario, tarea)
        modelo = MODELO_TRADUCCION if tarea == "traducir" else MODELO_AJUSTE
        max_tokens = max_tokens or (16000 if tarea == "traducir" else 8000)   # Haiku piensa antes de responder: con 2000 se quedaba sin espacio
        vacias = 0
        for intento in range(5):
            self.uso.revisar()
            try:
                r = self.cliente.messages.create(
                    model=modelo, max_tokens=max_tokens, system=sistema,
                    messages=[{"role": "user", "content": f"ENTRADA:\n{usuario}"}])
                self.uso.sumar(modelo, r.usage.input_tokens, r.usage.output_tokens)
                salida = "".join(getattr(b, "text", "") for b in r.content).strip()
                if salida:
                    return salida
                tipos = ",".join(getattr(b, "type", "?") for b in r.content) or "sin bloques"
                error = (f"respuesta vacía (modelo {modelo}, motivo {getattr(r, 'stop_reason', '?')}, "
                         f"contenido: {tipos}, tokens salida {r.usage.output_tokens})")
                vacias += 1
                if tarea != "traducir" and vacias >= 3:
                    # una reescritura o revisión vacía no debe detener todo el idioma: el bloque sigue
                    # con su texto actual y queda anotado en el reporte
                    log(f"   Claude: {error}; se sigue sin este paso")
                    raise RespuestaVacia(error)
            except RespuestaVacia:
                raise
            except Exception as e:
                codigo = getattr(e, "status_code", None)
                texto = str(e)
                error = f"{type(e).__name__} {codigo or ''}: {texto[:300]}"
                if codigo in (401, 403):
                    sys.exit("La API rechazó la clave. Revisa ANTHROPIC_API_KEY en el archivo .env\n" + error)
                if "credit" in texto.lower():
                    sys.exit("Se acabó el crédito de la API de Claude. Recarga en la consola y ejecuta el mismo "
                             "comando: retoma donde quedó.\n" + error)
                if codigo == 404:
                    sys.exit(f"No existe el modelo '{modelo}'. Revisa MODELO_TRADUCCION / MODELO_AJUSTE.\n" + error)
                if codigo == 400:
                    sys.exit("La API rechazó la solicitud:\n" + error)
            if intento == 4:
                sys.exit(f"La API de Claude falló varias veces: {error}\nVuelve a ejecutar: retoma donde quedó.")
            log(f"   Claude falló ({error.strip()[:150]}); reintento en {15 * (intento + 1)} s")
            time.sleep(15 * (intento + 1))

    def _pedir_pro(self, sistema, usuario, tarea="ajuste"):
        modelo = MODELO_CLAUDE if tarea == "traducir" else MODELO_CLAUDE_AJUSTE
        prompt = f"{sistema}\n\n---\nENTRADA:\n{usuario}"
        for intento in range(4):
            try:
                r = subprocess.run(["claude", "-p", "--model", modelo, "--output-format", "json"], input=prompt,
                                   capture_output=True, text=True, timeout=900,
                                   cwd=self.carpeta, env=self.env)
                salida = r.stdout.strip()
                try:                     # la respuesta JSON trae el texto, el modelo exacto y los tokens
                    d = json.loads(salida)
                    salida = (d.get("result") or "").strip()
                    if d.get("is_error"):
                        salida, r.returncode = "", 1
                        r.stderr = (r.stderr or "") + str(d.get("result", ""))
                    for mod, u in (d.get("modelUsage") or {}).items():
                        self.estimado.sumar(mod, u.get("inputTokens", 0), u.get("outputTokens", 0))
                        if mod not in self.modelos_vistos:
                            self.modelos_vistos.add(mod)
                            log(f"   (modelo real usado: {mod})")
                except (ValueError, AttributeError, TypeError):
                    pass                 # si no vino JSON, se usa el texto tal cual
                if r.returncode == 0 and salida:
                    return salida
                error = (r.stderr or salida)[-400:]
            except subprocess.TimeoutExpired:
                error = "tardó demasiado"
            if "limit" in error.lower():
                sys.exit("Se alcanzó el límite de uso de tu plan Pro. Vuelve a ejecutar el mismo comando "
                         "en unas horas: retoma donde quedó.\n" + error)
            if intento == 3:
                sys.exit(f"Claude Code falló varias veces: {error}")
            log(f"   Claude falló ({error.strip()[:150]}); reintento en {15 * (intento + 1)} s")
            time.sleep(15 * (intento + 1))


SISTEMA_TRADUCCION = """Eres traductor y adaptador de guiones de documentales de divulgación para doblaje.
Traduces narración en español latinoamericano al {idioma} para que la lea una voz sintética.
Reglas:
- Suena como un narrador nativo de documentales, natural y con ritmo; no traduzcas palabra por palabra.
- Cada bloque son frases completas y, leído en voz alta a ritmo normal de narrador, debe durar casi lo mismo que el original ("seg"): ni mucho más ni mucho menos.
- Si el {idioma} suele ser más largo, condensa sin perder información.
- Si tu traducción queda claramente más corta que el original (pasa mucho en inglés, francés o vietnamita), no la dejes así: usa una formulación más completa y natural, con las conexiones y matices que diría un narrador nativo, para acercarte al tiempo. Nunca inventes datos nuevos ni agregues relleno vacío.
- Cada bloque se traduce completo dentro de su propio "texto": no muevas ideas de un bloque a otro.
- Escribe los números, años, fechas, unidades y símbolos en palabras (ej.: "1.200 km" -> la forma hablada en {idioma}).
- Nombres propios: usa la forma habitual en {idioma}.
- Sin acotaciones, sin notas, sin comillas extra.
- No uses herramientas ni archivos: responde directamente.
- Devuelve SOLO un JSON: lista de objetos {{"id": <número>, "texto": "<traducción>"}} con los mismos id."""


class RespuestaVacia(Exception):
    """Claude respondió sin texto varias veces seguidas en una tarea que se puede saltar."""


def traducir(claude, bloques, idioma, glosario):
    nombre = NOMBRES.get(idioma, idioma)
    sistema = SISTEMA_TRADUCCION.format(idioma=nombre)
    if glosario:
        sistema += f"\n\nGlosario / contexto del video:\n{glosario}"
    resultado = {}
    lote = 40
    for i in range(0, len(bloques), lote):
        parte = bloques[i:i + lote]
        entrada = [{"id": b["id"], "seg": round(b["fin"] - b["inicio"], 1), "texto": b["texto"]} for b in parte]
        for intento in range(3):
            try:
                resp = extraer_json(claude.pedir(sistema, json.dumps(entrada, ensure_ascii=False), tarea="traducir"))
                if {r["id"] for r in resp} == {b["id"] for b in parte}:
                    break
            except (ValueError, KeyError, TypeError):
                pass
            log("   la traducción vino incompleta; reintento")
        else:
            sys.exit("No se pudo traducir un lote completo. Vuelve a ejecutar.")
        resultado.update({str(r["id"]): r["texto"].strip() for r in resp})
        log(f"   traducidos {min(i + lote, len(bloques))}/{len(bloques)} bloques")
    return resultado


def reformular(claude, texto, idioma, factor, original_es):
    """Reescribe la narración con otra estructura para que dure `factor` veces lo actual
    (menos de 1 = más corta, más de 1 = más larga), sin cambiar la velocidad de la voz."""
    nombre = NOMBRES.get(idioma, idioma)
    pct = abs(int(round((factor - 1) * 100)))
    if factor < 1:
        meta = (f"un {pct} % más corta al leerla en voz alta. Cuenta lo mismo con otra estructura: "
                "frases más directas, palabras más cortas, sin repetir ideas. No pierdas información clave")
    else:
        meta = (f"un {pct} % más larga al leerla en voz alta. Cuenta lo mismo con una estructura más completa "
                "y natural, con las conexiones y matices que diría un narrador nativo de documentales. "
                "No inventes datos nuevos ni agregues relleno vacío")
    sistema = (f"Reescribe esta narración en {nombre} para que sea {meta}. Debe seguir siendo fiel al "
               "original en español. Números en palabras. No uses herramientas. Devuelve SOLO el texto.")
    usuario = json.dumps({"original_es": original_es, "texto_actual": texto}, ensure_ascii=False)
    try:
        return claude.pedir(sistema, usuario).strip()
    except RespuestaVacia:
        return texto                     # se queda con el texto actual; ajustar_bloque decide con lo que hay


def revisar(claude, original_es, texto, escuchado, idioma):
    nombre = NOMBRES.get(idioma, idioma)
    sistema = f"""Eres control de calidad de doblaje en {nombre}.
Te doy: el texto original en español, el texto en {nombre} que debía leer la voz sintética, y lo que un reconocedor de voz entendió del audio generado.
Ignora diferencias de puntuación, números escritos en cifras vs. palabras y pequeñas variaciones que no cambian lo que se oye.
Decide si el audio dice correctamente el texto. Si hay un problema (palabra mal leída, omitida, nombre mal pronunciado, idioma equivocado), propone una versión del texto en {nombre} que evite el problema (por ejemplo, escribir un nombre como se pronuncia o reformular la frase).
No uses herramientas. Devuelve SOLO un JSON: {{"ok": true/false, "problema": "<explicación breve en español>", "texto_corregido": "<texto en {nombre} o vacío>", "se_entiende_en_espanol": "<traducción al español de lo que se escuchó>"}}"""
    usuario = json.dumps({"original_es": original_es, "texto_esperado": texto, "audio_entendido": escuchado},
                         ensure_ascii=False)
    try:
        return extraer_json(claude.pedir(sistema, usuario))
    except RespuestaVacia:
        return {"ok": False, "problema": "Claude no respondió a la revisión (revisar a oído)",
                "texto_corregido": "", "se_entiende_en_espanol": ""}
    except Exception:
        return {"ok": False, "problema": "No se pudo revisar automáticamente", "texto_corregido": "",
                "se_entiende_en_espanol": ""}


# ---------------------------------------------------------------- título y descripción por idioma

MAX_TITULO = 100          # límites de la plataforma de destino
MAX_DESCRIPCION = 5000    # bytes UTF-8 (no caracteres)
RE_ENLACE = re.compile(r"https?://[^\s<>]+")
RE_MARCA = re.compile(r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?(?![\d:])")
RE_HASHTAG = re.compile(r"#\w+")

SISTEMA_META = """Traduces el título y la descripción de un video de divulgación del español al {idioma}.
Reglas:
- Suena natural, como lo escribiría un creador nativo. El título es claro y atractivo, de {max_titulo} caracteres como máximo.
- Conserva EXACTAMENTE, sin traducir ni modificar: los enlaces (http...), las marcas de tiempo (0:00, 12:34, 1:02:03) y los hashtags (#algo).
- Conserva los saltos de línea y las líneas en blanco: cada línea del original sigue siendo una línea. En una lista de capítulos traduce solo el texto que sigue a la marca de tiempo.
- Nunca uses los signos < ni >.
- La descripción no puede pasar de 5000 bytes en UTF-8 (en japonés, ruso o árabe cada carácter pesa 2 o 3 bytes): si hace falta, resume el texto, nunca los enlaces, las marcas de tiempo ni los hashtags.
- No agregues ni quites información. Sin notas ni comentarios.
- No uses herramientas ni archivos: responde directamente.
- Devuelve SOLO un JSON: {{"title": "<título>", "description": "<descripción>"}}"""


def leer_titulo(ruta):
    """titulo.txt: línea 1 = título en español; desde la línea 3 = descripción. None si no hay o está vacío."""
    if not ruta or not Path(ruta).exists():
        return None
    lineas = Path(ruta).read_text("utf-8-sig").splitlines()
    titulo = lineas[0].strip() if lineas else ""
    if not titulo:
        return None
    return {"title": titulo, "description": "\n".join(lineas[2:]).strip()}


def _enlaces(texto):
    return {u.rstrip(".,;:!?)»\"'") for u in RE_ENLACE.findall(texto)}


def problemas_meta(origen, meta):
    """Lista de problemas de una traducción de título/descripción (vacía = está bien)."""
    t, d = meta["title"], meta["description"]
    p = []
    if not t.strip():
        p.append("el título quedó vacío")
    if len(t) > MAX_TITULO:
        p.append(f"el título tiene {len(t)} caracteres (máximo {MAX_TITULO})")
    if len(d.encode("utf-8")) > MAX_DESCRIPCION:
        p.append(f"la descripción pesa {len(d.encode('utf-8'))} bytes en UTF-8 (máximo {MAX_DESCRIPCION}): "
                 "acórtala resumiendo el texto, sin tocar enlaces, marcas de tiempo ni hashtags")
    if "<" in t + d or ">" in t + d:
        p.append("hay signos < o >")
    ori, tra = origen["title"] + "\n" + origen["description"], t + "\n" + d
    p += [f"falta el enlace {u}" for u in sorted(_enlaces(ori)) if u not in tra]
    faltan = Counter(RE_MARCA.findall(ori)) - Counter(RE_MARCA.findall(tra))
    p += [f"falta la marca de tiempo {m}" for m in faltan]
    faltan = Counter(RE_HASHTAG.findall(ori)) - Counter(RE_HASHTAG.findall(tra))
    p += [f"falta el hashtag {h}" for h in faltan]
    lineas = lambda x: len([l for l in x.splitlines() if l.strip()])
    if lineas(d) != lineas(origen["description"]):
        p.append(f"la descripción tiene {lineas(d)} líneas y el original {lineas(origen['description'])}")
    return p


def _recortar(texto, limite, en_bytes=False):
    """Último recurso: corta en un espacio o salto de línea (en bytes UTF-8 si en_bytes)."""
    medida = (lambda t: len(t.encode("utf-8"))) if en_bytes else len
    if medida(texto) <= limite:
        return texto
    corte = texto
    while medida(corte + "…") > limite:
        corte = corte[:-1]
    pos = max(corte.rfind(" "), corte.rfind("\n"))
    if pos > len(corte) // 2:
        corte = corte[:pos]
    return corte.rstrip() + "…"


def _recortar_descripcion(texto):
    """Último recurso para pasar de MAX_DESCRIPCION bytes: quita párrafos de texto desde el final y nunca
    las líneas con enlaces, marcas de tiempo, hashtags ni encabezados que terminan en dos puntos."""
    lineas = texto.split("\n")
    protegida = lambda l: (RE_ENLACE.search(l) or RE_MARCA.search(l) or RE_HASHTAG.search(l)
                           or l.rstrip().endswith((":", "：")))     # encabezados como "Capítulos:"
    while len("\n".join(lineas).encode("utf-8")) > MAX_DESCRIPCION:
        for i in range(len(lineas) - 1, -1, -1):
            if lineas[i].strip() and not protegida(lineas[i]):
                del lineas[i]
                break
        else:
            break
    texto = re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()
    return _recortar(texto, MAX_DESCRIPCION, en_bytes=True)      # solo actúa si aún se pasa


def traducir_meta(claude, origen, idioma, glosario):
    """Devuelve (meta, avisos). Reintenta con los problemas encontrados; el título largo se recorta al final."""
    nombre = NOMBRES.get(idioma, idioma)
    sistema = SISTEMA_META.format(idioma=nombre, max_titulo=MAX_TITULO)
    if glosario:
        sistema += f"\n\nGlosario / contexto del video:\n{glosario}"
    entrada = json.dumps(origen, ensure_ascii=False)
    meta, probs, aviso = None, ["no se obtuvo traducción"], ""
    for _ in range(3):
        try:
            r = extraer_json(claude.pedir(sistema + aviso, entrada, max_tokens=16000))
            meta = {"title": " ".join(str(r["title"]).split()).replace("<", "").replace(">", ""),
                    "description": str(r["description"]).strip().replace("<", "").replace(">", "")}
        except Exception:            # respuesta vacía, JSON roto, error de red: se reintenta
            continue
        probs = problemas_meta(origen, meta)
        if not probs:
            return meta, []
        aviso = "\n\nEn tu intento anterior hubo estos problemas; corrígelos: " + "; ".join(probs)
    if meta is None:
        return {"title": "", "description": ""}, probs
    avisos = []
    titulo = _recortar(meta["title"], MAX_TITULO)
    if titulo != meta["title"]:
        avisos.append(f"título recortado de {len(meta['title'])} a {len(titulo)} caracteres")
    descr = _recortar_descripcion(meta["description"])
    if descr != meta["description"]:
        avisos.append(f"descripción recortada de {len(meta['description'].encode('utf-8'))} a "
                      f"{len(descr.encode('utf-8'))} bytes quitando párrafos de texto; se conservaron enlaces, marcas de tiempo y hashtags (revisa)")
    meta["title"], meta["description"] = titulo, descr
    return meta, avisos + problemas_meta(origen, meta)


def generar_meta(claude, idioma, origen, glosario, salida, cache):
    """Traduce título y descripción a meta_XX.json (con caché: si el original no cambió, no se paga de nuevo)."""
    ruta_cache = cache / "titulo.json"
    hecho = leer_json(ruta_cache)
    if hecho is None or hecho.get("origen") != origen:
        log("-> Título y descripción")
        meta, avisos = traducir_meta(claude, origen, idioma, glosario)
        hecho = {"origen": origen, "meta": meta, "avisos": avisos}
        if meta["title"]:               # un fallo total no se guarda: un retomar lo vuelve a intentar
            guardar_json(ruta_cache, hecho)
    for a in hecho["avisos"]:
        log(f"!! título/descripción ({idioma}): {a}")
    if hecho["meta"]["title"]:
        guardar_json(Path(salida) / f"meta_{idioma}.json", hecho["meta"])
    return hecho["avisos"]


# ---------------------------------------------------------------- contador mensual y ritmo de la voz

def caracteres_facturables(texto):
    """Azure cuenta cada ideograma (hanzi/kanji) como 2 caracteres."""
    extra = sum(1 for c in texto if "一" <= c <= "鿿" or "㐀" <= c <= "䶿")
    return len(texto) + extra


class UsoMensual:
    """Suma los caracteres enviados a Azure en el mes (se guarda en uso_azure.json)."""

    def __init__(self, ruta=RUTA_USO, limite=LIMITE_MENSUAL):
        self.ruta = Path(ruta)
        self.limite = limite
        self.mes = time.strftime("%Y-%m")
        self.datos = leer_json(self.ruta, {})
        self.avisado = False

    @property
    def total(self):
        return self.datos.get(self.mes, 0)

    def revisar(self, n):
        if self.total + n > self.limite:
            sys.exit(f"\nSe alcanzaría el límite de {self.limite:,} caracteres de Azure de este mes "
                     f"(llevas {self.total:,}). El avance quedó guardado: vuelve a ejecutar el mismo comando "
                     f"el mes que viene. (Si pasaste a un plan de pago, sube LIMITE_AZURE.)")
        if not self.avisado and self.total + n > 0.9 * self.limite:
            self.avisado = True
            log(f"!! Aviso: llevas {self.total:,} de {self.limite:,} caracteres de Azure este mes (más del 90 %).")

    def sumar(self, n):
        self.datos[self.mes] = self.total + n
        guardar_json(self.ruta, self.datos)


USO = UsoMensual()


class TasaVoz:
    """Aprende cuántos segundos dura la voz por carácter (a velocidad normal) para este idioma."""

    def __init__(self, ruta):
        self.ruta = ruta
        d = leer_json(ruta, {"seg": 0.0, "chars": 0})
        self.seg, self.chars = d["seg"], d["chars"]

    def estimar(self, n):
        if self.chars < 200:      # todavía sin datos suficientes
            return None
        return n * self.seg / self.chars

    def registrar(self, n, duracion_s, velocidad):
        self.seg += duracion_s * (1 + velocidad / 100)   # lo pasa a velocidad normal
        self.chars += n
        guardar_json(self.ruta, {"seg": self.seg, "chars": self.chars})


# ---------------------------------------------------------------- Azure

class Azure:
    def __init__(self, cache):
        self.key = os.environ["AZURE_SPEECH_KEY"]
        self.region = os.environ["AZURE_SPEECH_REGION"]
        self.cache = cache
        self.caracteres = 0

    def generar(self, texto, voz, velocidad=0):
        clave = hashlib.sha1(f"{voz}|{velocidad}|{texto}".encode()).hexdigest()[:16]
        salida = self.cache / f"{clave}.wav"
        if salida.exists():
            return salida
        n_fact = caracteres_facturables(texto)
        USO.revisar(n_fact)
        locale = "-".join(voz.split("-")[:2])
        ssml = (f"<speak version='1.0' xml:lang='{locale}' xmlns='http://www.w3.org/2001/10/synthesis'>"
                f"<voice name='{voz}'><prosody rate='{velocidad:+d}%'>{html.escape(texto)}</prosody>"
                f"</voice></speak>")
        url = f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1"
        cab = {"Ocp-Apim-Subscription-Key": self.key, "Content-Type": "application/ssml+xml",
               "X-Microsoft-OutputFormat": "riff-48khz-16bit-mono-pcm", "User-Agent": "sin-coraza-doblaje"}
        for intento in range(8):
            r = requests.post(url, headers=cab, data=ssml.encode("utf-8"), timeout=120)
            if r.status_code == 200:
                salida.write_bytes(r.content)
                self.caracteres += n_fact
                USO.sumar(n_fact)
                return salida
            if r.status_code in (429, 500, 503):  # límite de la capa gratis: esperar
                time.sleep(5 * (intento + 1))
                continue
            sys.exit(f"Azure respondió {r.status_code}: {r.text[:300]}\n"
                     "Revisa AZURE_SPEECH_KEY / AZURE_SPEECH_REGION en el archivo .env")
        sys.exit("Azure no respondió después de varios intentos (límite de la capa gratis). Vuelve a ejecutar más tarde.")


FILTRO_EDGE = ("aresample=48000,highpass=f=70,equalizer=f=180:t=q:w=1:g=2.5,equalizer=f=3000:t=q:w=1.2:g=1.5,"
               "aexciter=amount=1.5:drive=5:freq=6000:ceil=16000,"
               "acompressor=threshold=0.1:ratio=3:attack=5:release=80:makeup=1.5,alimiter=limit=0.95")


class Edge:
    """v5: voces gratis de Microsoft Edge (edge-tts). Si falla, el bloque se genera con Azure (respaldo)."""

    def __init__(self, cache):
        self.cache = cache
        self.caracteres = 0          # caracteres que terminaron yendo a Azure (respaldo)
        self.caracteres_edge = 0     # caracteres generados gratis con edge-tts
        self.fallos_seguidos = 0
        self._azure = None

    def _respaldo(self, texto, voz, velocidad):
        if self._azure is None:
            if not os.environ.get("AZURE_SPEECH_KEY"):
                sys.exit("edge-tts no responde y no hay AZURE_SPEECH_KEY para el respaldo. "
                         "Vuelve a ejecutar más tarde: retoma donde quedó.")
            self._azure = Azure(self.cache)
        wav = self._azure.generar(texto, voz, velocidad)
        self.caracteres = self._azure.caracteres
        return wav

    def generar(self, texto, voz, velocidad=0):
        clave = hashlib.sha1(f"{voz}|{velocidad}|{texto}".encode()).hexdigest()[:16]
        salida = self.cache / f"{clave}.wav"
        if salida.exists():
            return salida
        if self.fallos_seguidos >= 3:            # edge-tts caído: no perder tiempo reintentando
            return self._respaldo(texto, voz, velocidad)
        try:
            import edge_tts
        except ImportError:
            sys.exit("Falta edge-tts. Ejecuta: source venv/bin/activate && pip install edge-tts")
        ultimo = None
        for intento in range(4):
            try:
                with tempfile.TemporaryDirectory() as d:
                    mp3 = Path(d) / "voz.mp3"
                    asyncio.run(edge_tts.Communicate(texto, voz, rate=f"{velocidad:+d}%").save(str(mp3)))
                    if not mp3.exists() or mp3.stat().st_size < 500:
                        raise RuntimeError("edge-tts devolvió un audio vacío")
                    if os.environ.get("VOZ_FILTRO", "1") == "0":
                        a_wav(mp3, salida, 1)
                    else:                        # v5.1: devuelve cuerpo y brillo que pierde el MP3 de edge-tts
                        ffmpeg("-i", str(mp3), "-af", FILTRO_EDGE, "-ac", "1", "-ar", str(SR),
                               "-c:a", "pcm_s16le", str(salida))
                self.caracteres_edge += len(texto)
                self.fallos_seguidos = 0
                return salida
            except Exception as e:
                ultimo = e
                time.sleep(3 * (intento + 1))
        self.fallos_seguidos += 1
        log(f"!! edge-tts falló ({ultimo}). Este bloque va por Azure (respaldo).")
        return self._respaldo(texto, voz, velocidad)


def duracion(ruta):
    return sf.info(str(ruta)).duration


# ---------------------------------------------------------------- ajuste de tiempo de un bloque

def ajustar_bloque(texto, espacio, azure, voz, claude, idioma, tasa, reg, dur_es, original_es):
    """v3.1: genera la voz a velocidad normal y reescribe el texto (más largo o más corto) hasta que dure
    casi lo mismo que el español. Guarda TODAS las versiones generadas y al final elige la mejor:
    la que más llena el espacio sin pasarse (o pasándose poco, con hasta +VEL_EMERGENCIA % de velocidad).
    Así una versión alargada que se pasa un poco ya no se recorta de más."""
    ideal = max(min(espacio - 0.3, dur_es), 0.5)
    limite = espacio * (1 + VEL_EMERGENCIA / 100)
    candidatos = []                     # (texto, wav, duración)

    def probar(t):
        w = azure.generar(t, voz, 0)
        dd = duracion(w)
        tasa.registrar(len(t), dd, 0)
        candidatos.append((t, w, dd))

    def efectiva(c):                    # lo que dura una vez ajustada (si se pasa, se acelera hasta el espacio)
        return min(c[2], espacio)

    def mejor():
        validos = [c for c in candidatos if c[2] <= limite]
        if not validos:
            return None
        # el que más llena; a igualdad, el que no necesita acelerar
        return max(validos, key=lambda c: (round(efectiva(c), 1), -c[2]))

    est = tasa.estimar(len(texto))
    if est is not None and (est > espacio * 0.97 or ideal - est > HUECO_ALARGAR):
        texto = reformular(claude, texto, idioma, ideal / est, original_es)
        reg["notas"].append("reescrito más corto" if est > ideal else "reescrito más largo")
    probar(texto)

    for intento in range(2):            # corregir con el audio real (v3: 3)
        m = mejor()
        if m is not None and ideal - efectiva(m) <= HUECO_ALARGAR:
            break
        if m is None:                   # todas se pasan: acortar la más corta
            base = min(candidatos, key=lambda c: c[2])
            factor = ideal * 0.97 / base[2]
        else:                           # cabe pero queda corta: alargar la mejor
            base = m
            factor = ideal / base[2]
        probar(reformular(claude, base[0], idioma, factor, original_es))
        reg["notas"].append("reescrito más corto" if factor < 1 else "reescrito más largo")

    m = mejor() or min(candidatos, key=lambda c: c[2])
    texto, wav, d = m
    vel = 0
    if d > espacio and VEL_EMERGENCIA > 0:   # último recurso, leve y anotado en el reporte
        vel = min(int((d / espacio - 1) * 100) + 1, VEL_EMERGENCIA)
        wav = azure.generar(texto, voz, vel)
        reg["notas"].append(f"velocidad de emergencia +{vel} %")
    return texto, vel, wav


# ---------------------------------------------------------------- proceso por idioma

def doblar_idioma(idioma, bloques, args, trabajo, claude, glosario, total, ganancia_ref):
    voz = VOCES.get(idioma)
    if not voz:
        log(f"!! No hay voz definida para '{idioma}'. Agrégala en VOCES. Se omite.")
        return
    carpeta = trabajo / idioma
    carpeta.mkdir(exist_ok=True)
    azure = Edge(trabajo / "cache_voz") if VOZ_MOTOR == "edge" else Azure(trabajo / "cache_voz")
    log(f"\n=== {NOMBRES.get(idioma, idioma).upper()} ({voz}, {'edge-tts' if VOZ_MOTOR == 'edge' else 'Azure'}) ===")
    gasto_inicio = claude.medidor.total
    t_inicio = time.time()
    TIEMPOS.clear()

    # 1) traducción (editable a mano en traduccion.json)
    ruta_tr = carpeta / "traduccion.json"
    traduccion = leer_json(ruta_tr)
    if traduccion is None:
        log("-> Traduciendo")
        traduccion = traducir(claude, bloques, idioma, glosario)
        guardar_json(ruta_tr, traduccion)

    # 2) voz + ajuste de tiempo + control de calidad, bloque a bloque (retomable)
    ruta_estado = carpeta / "estado.json"
    estado = leer_json(ruta_estado, {})
    tasa = TasaVoz(carpeta / "tasa.json")
    log("-> Generando voz y revisando")
    for i, b in enumerate(bloques):
        k = str(b["id"])
        if k in estado:
            continue
        sig = bloques[i + 1]["inicio"] if i + 1 < len(bloques) else total
        espacio = max(sig - b["inicio"] - MARGEN, 0.5)
        texto = traduccion[k]
        reg = {"texto": texto, "velocidad": 0, "notas": []}

        def ajustar(t):
            return ajustar_bloque(t, espacio, azure, voz, claude, idioma, tasa, reg,
                                  b["fin"] - b["inicio"], b["texto"])

        texto, vel, wav = ajustar(texto)

        escuchado = transcribir(wav, idioma)["text"].strip()
        err = diferencia(texto, escuchado, idioma)
        if err > UMBRAL_ERROR:
            rev = revisar(claude, b["texto"], texto, escuchado, idioma)
            if not rev.get("ok") and rev.get("texto_corregido"):
                texto, vel, wav = ajustar(rev["texto_corregido"])
                escuchado = transcribir(wav, idioma)["text"].strip()
                err = diferencia(texto, escuchado, idioma)
                reg["notas"].append("corregido tras revisión")
                if err > UMBRAL_ERROR:
                    rev = revisar(claude, b["texto"], texto, escuchado, idioma)
            if not rev.get("ok"):
                reg["dudoso"] = {"problema": rev.get("problema", ""),
                                 "se_entiende": rev.get("se_entiende_en_espanol", ""),
                                 "escuchado": escuchado}
        reg.update({"texto": texto, "velocidad": vel, "wav": wav.name, "error": round(err, 3)})
        estado[k] = reg
        guardar_json(ruta_estado, estado)
        if (i + 1) % 20 == 0 or i + 1 == len(bloques):
            log(f"   {i + 1}/{len(bloques)} bloques")

    # 3) montaje en la línea de tiempo: PISTA DE VOZ sola (la mezcla con el fondo se hace en "mezclar")
    log("-> Armando la pista de voz")
    pista = np.zeros(int(total * SR) + SR, dtype=np.float32)
    cursor, desfases, huecos, colocados = 0.0, 0, [], []
    lista_desfases = []
    for i, b in enumerate(bloques):
        reg = estado[str(b["id"])]
        audio, _ = sf.read(str(trabajo / "cache_voz" / reg["wav"]), dtype="float32")
        inicio = max(b["inicio"], cursor + 0.1)
        if inicio - b["inicio"] > 1.0:
            desfases += 1
            lista_desfases.append((b["inicio"], b["id"], inicio - b["inicio"]))
        s = int(inicio * SR)
        e = min(s + len(audio), len(pista))
        if s < len(pista):
            pista[s:e] += audio[:e - s]
        cursor = inicio + len(audio) / SR
        colocados.append({"id": b["id"], "inicio": round(inicio, 3), "fin": round(cursor, 3), "texto": reg["texto"]})
        if i + 1 < len(bloques):
            hueco = bloques[i + 1]["inicio"] - cursor
            pausa_es = bloques[i + 1]["inicio"] - b["fin"]     # la pausa que ya tenía el español
            if hueco > HUECO_REPORTE and hueco - pausa_es > 2:
                huecos.append((cursor, bloques[i + 1]["inicio"]))
    if cursor > total + 0.05:
        log(f"!! La voz doblada termina en {cursor:.1f} s y la locución dura {total:.1f} s (se alarga la pista).")
    pista = pista[:max(int(total * SR), int(cursor * SR) + 1)]

    activo = pista[np.abs(pista) > 1e-3]
    if len(activo):
        pista *= ganancia_ref / np.sqrt(np.mean(activo ** 2))
    pico = float(np.max(np.abs(pista))) if len(pista) else 0.0
    escala = 0.98 / pico if pico > 0.98 else 1.0          # FLAC no admite valores > 1: se guarda la escala
    salida = Path(args.salida)
    sf.write(str(salida / f"pista_voz_{idioma}.flac"), pista * escala, SR, subtype="PCM_24")
    guardar_json(salida / f"pista_voz_{idioma}.json", {"escala": escala, "duracion": len(pista) / SR,
                                                       "bloques": colocados})

    # subtítulos del idioma (misma colocación que la pista)
    import fmt as hacer_srt
    subs = []
    for c in colocados:
        subs += hacer_srt.partir_en_subtitulos(c["texto"], c["inicio"], c["fin"], idioma)
    (salida / "subtitulos").mkdir(exist_ok=True)
    hacer_srt.escribir(salida / "subtitulos" / hacer_srt.nombre_archivo(args.video, idioma), subs)

    # 4) reporte
    dudosos = [(b, estado[str(b["id"])]) for b in bloques if "dudoso" in estado[str(b["id"])]]
    gasto = claude.medidor.total - gasto_inicio
    etiqueta = "API de Claude" if claude.motor == "api" else "Costo ESTIMADO si fuera API (corrió con el Pro, no se pagó)"
    lineas = [f"# Reporte de doblaje — {NOMBRES.get(idioma, idioma)} (nube)", "",
              f"- Bloques: {len(bloques)}",
              f"- Dudosos tras la revisión automática: {len(dudosos)}",
              f"- Bloques desplazados más de 1 s respecto al original: {desfases}",
              f"- Silencios nuevos de más de {HUECO_REPORTE:.0f} s (que el español no tenía): {len(huecos)}",
              f"- Caracteres generados gratis con edge-tts: {getattr(azure, 'caracteres_edge', 0):,}",
              f"- Caracteres enviados a Azure en esta ejecución: {azure.caracteres:,}",
              f"- Caracteres de Azure acumulados este mes: {USO.total:,} de {USO.limite:,}",
              f"- {etiqueta}: {gasto:.3f} USD en esta ejecución", ""]
    emerg = [(b, estado[str(b["id"])]["velocidad"]) for b in bloques if estado[str(b["id"])]["velocidad"]]
    reesc = sum(1 for b in bloques if any("reescrito" in n for n in estado[str(b["id"])]["notas"]))
    lineas.insert(-1, f"- Bloques reescritos para durar lo mismo (más cortos o más largos): {reesc}")
    lineas.insert(-1, f"- Bloques con velocidad cambiada (último recurso, máx. +{VEL_EMERGENCIA} %): {len(emerg)}")
    total_s = time.time() - t_inicio
    if total_s > 1:
        lineas += ["## Tiempo por paso (en esta ejecución)", ""]
        for nombre, seg in sorted(TIEMPOS.items(), key=lambda x: -x[1]):
            lineas.append(f"- {nombre}: {seg / 60:.1f} min ({seg / total_s * 100:.0f} %)")
        otros = max(total_s - sum(TIEMPOS.values()), 0)
        lineas += [f"- Otros (filtro, archivos): {otros / 60:.1f} min", f"- **Total: {total_s / 60:.1f} min**", ""]
    if emerg:
        lineas += ["## Velocidad cambiada (escuchar)", ""]
        lineas += [f"- {mmss(b['inicio'])} (id {b['id']}): +{v} %" for b, v in emerg]
        lineas.append("")
    if lista_desfases:
        lineas += ["## Bloques desplazados (escuchar)", ""]
        lineas += [f"- {mmss(t)} (id {i}): +{d:.1f} s" for t, i, d in lista_desfases]
        lineas.append("")
    if huecos:
        lineas += ["## Silencios para escuchar", ""]
        lineas += [f"- {mmss(a)} a {mmss(z)} ({z - a:.1f} s)" for a, z in huecos]
        lineas.append("")
    if dudosos:
        lineas += ["## Bloques a revisar", "",
                   "Para corregir: edita el texto en traduccion.json, borra ese id de estado.json y vuelve a ejecutar.", ""]
        for b, reg in dudosos:
            d = reg["dudoso"]
            lineas += [f"### {mmss(b['inicio'])} (id {b['id']})",
                       f"- Original: {b['texto']}",
                       f"- Debía decir: {reg['texto']}",
                       f"- Se entendió: {d['escuchado']}",
                       f"- En español se entiende: {d['se_entiende']}",
                       f"- Problema: {d['problema']}", ""]
    else:
        lineas.append("Sin bloques dudosos.")
    (Path(args.salida) / f"reporte_{idioma}.md").write_text("\n".join(lineas), "utf-8")
    log(f"OK -> pista_voz_{idioma}.flac  ({len(dudosos)} dudosos, {len(huecos)} silencios largos, "
        f"{getattr(azure, 'caracteres_edge', 0):,} caracteres gratis en edge-tts, "
        f"{azure.caracteres:,} caracteres en Azure)")
    log(f"   Este mes llevas {USO.total:,} de {USO.limite:,} caracteres de Azure.")
    log(f"   {etiqueta}: {gasto:.3f} USD en este idioma.")


# ---------------------------------------------------------------- comandos

def cmd_preparar(args):
    salida = Path(args.salida)
    salida.mkdir(parents=True, exist_ok=True)
    caps = capitulos(args.capitulos)
    log(f"-> {len(caps)} capítulos")
    voz = unir_capitulos(caps, salida / "voz_es.wav")
    total = duracion(voz)
    ruta_bloques = salida / "bloques_es.json"
    bloques = leer_json(ruta_bloques)
    if bloques is None:
        # tiempos de cada capítulo: del .json de ElevenLabs si está y calza; si no, Whisper (solo ese capítulo)
        piezas, offset, con_whisper = [], 0.0, []
        for n, wav, txt in caps:
            ok, motivo = json_valido(wav.with_suffix(".json"), txt, wav)
            if ok:
                piezas += piezas_desde_elevenlabs(wav.with_suffix(".json"), offset)
            else:
                con_whisper.append(f"cap{n:02d} ({motivo})")
                segs = transcribir(wav, "es", con_palabras=True)["segments"]
                for sg in segs:
                    sg["start"] += offset
                    sg["end"] += offset
                piezas += [p for sg in segs if sg["text"].strip() for p in piezas_de_segmento(sg)]
            offset += duracion(wav)
        if con_whisper:
            log(f"-> Tiempos: Whisper en {len(con_whisper)} de {len(caps)} capítulos: " + ", ".join(con_whisper))
        else:
            log(f"-> Tiempos: ElevenLabs en los {len(caps)} capítulos (sin Whisper)")
        bloques = bloques_desde_piezas(piezas)
        if con_whisper:
            bloques = quitar_alucinaciones(bloques, voz)
        log("-> Texto desde el guion (sin etiquetas <break>)")
        cobertura = alinear_con_guion(bloques, texto_guion(caps))
        log(f"   {cobertura:.0%} de las palabras del guion coinciden con los tiempos")
        if cobertura < 0.8:
            sys.exit("Menos del 80 % del guion coincide con la locución: ¿subiste el guion correcto?")
        guardar_json(ruta_bloques, bloques)
    voz_es, _ = sf.read(str(voz), dtype="float32")
    activo = voz_es[np.abs(voz_es) > 1e-3]
    meta = {"duracion": total, "ganancia_ref": float(np.sqrt(np.mean(activo ** 2))) if len(activo) else 0.1,
            "capitulos": [{"n": n, "archivo": w.name, "duracion": duracion(w)} for n, w, _ in caps]}
    guardar_json(salida / "meta.json", meta)
    import fmt as hacer_srt
    (salida / "subtitulos").mkdir(exist_ok=True)
    hacer_srt.escribir(salida / "subtitulos" / hacer_srt.nombre_archivo(args.video, "es"),
                       hacer_srt.srt_espanol(bloques))
    a_medias = sum(1 for b in bloques if not cierra(b["texto"]))
    log(f"{len(bloques)} bloques ({a_medias} sin punto final; el más largo dura "
        f"{max(b['fin'] - b['inicio'] for b in bloques):.1f} s). Locución: {total / 60:.1f} min.")


def cmd_idioma(args):
    salida = Path(args.salida)
    trabajo = salida / "_trabajo"
    (trabajo / "cache_voz").mkdir(parents=True, exist_ok=True)
    bloques = leer_json(salida / "bloques_es.json")
    meta = leer_json(salida / "meta.json")
    if bloques is None or meta is None:
        sys.exit(f"Falta bloques_es.json o meta.json en {salida}: corre primero 'preparar'.")
    if VOZ_MOTOR == "azure":
        for var in ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION"):
            if not os.environ.get(var):
                sys.exit(f"Falta {var}")
    glosario = Path(args.glosario).read_text("utf-8") if args.glosario and Path(args.glosario).exists() else ""
    claude = Claude()
    if claude.motor == "api":
        log(f"Claude: API ({MODELO_TRADUCCION} traduce, {MODELO_AJUSTE} reescribe y revisa). "
            f"Tope de este idioma: {claude.uso.tope:.2f} USD (gastado: {claude.uso.total:.2f}).")
    origen = leer_titulo(args.titulo)
    avisos_meta = None
    if origen:                       # sin titulo.txt se salta; un fallo aquí no detiene el doblaje
        try:
            (trabajo / args.idioma).mkdir(exist_ok=True)
            avisos_meta = generar_meta(claude, args.idioma, origen, glosario, salida, trabajo / args.idioma)
        except (Exception, SystemExit) as e:    # SystemExit: si la API falla, el doblaje lo dirá con su propio mensaje
            avisos_meta = [f"no se pudo traducir ({type(e).__name__}); el doblaje sigue"]
            log(f"!! título/descripción ({args.idioma}): {avisos_meta[0]}")
    doblar_idioma(args.idioma, bloques, args, trabajo, claude, glosario, meta["duracion"], meta["ganancia_ref"])
    if avisos_meta is not None:
        with open(salida / f"reporte_{args.idioma}.md", "a", encoding="utf-8") as f:
            f.write("\n\n## Título y descripción\n\n" +
                    ("\n".join(f"- Aviso: {a}" for a in avisos_meta) if avisos_meta else "- Traducidos sin problemas.") + "\n")


def cmd_titulos(args):
    """Solo título y descripción de cada idioma (videos ya doblados): no toca voz ni pistas."""
    origen = leer_titulo(args.titulo)
    if origen is None:
        sys.exit("No hay titulo.txt (o está vacío): línea 1 = título, desde la línea 3 = descripción.")
    salida = Path(args.salida)
    glosario = Path(args.glosario).read_text("utf-8") if args.glosario and Path(args.glosario).exists() else ""
    claude = Claude()
    for idioma in [x.strip() for x in args.idiomas.split(",") if x.strip()]:
        cache = salida / "_trabajo" / idioma
        cache.mkdir(parents=True, exist_ok=True)
        avisos = generar_meta(claude, idioma, origen, glosario, salida, cache)
        log(f"{idioma}: {'OK' if not avisos else str(len(avisos)) + ' aviso(s)'}")
    log(f"Costo de la API: {claude.medidor.total:.3f} USD")


def cmd_mezclar(args):
    """Mezcla cada pista de voz con el fondo. Protección: el fondo solo puede ser más largo AL FINAL (cola de
    cierre). Si es más corto, o más largo de lo razonable, se detiene (algo se cortó o movió en el montaje)."""
    salida = Path(args.salida)
    meta = leer_json(salida / "meta.json")
    with tempfile.TemporaryDirectory() as d:
        fondo_wav = a_wav(args.fondo, Path(d) / "fondo.wav", 2)
        fondo, _ = sf.read(str(fondo_wav), dtype="float32")
    dur_fondo, dur_voz = len(fondo) / SR, meta["duracion"]
    dif = dur_fondo - dur_voz
    log(f"Locución: {dur_voz:.2f} s · fondo: {dur_fondo:.2f} s · diferencia: {dif:+.2f} s")
    if dif < -0.05:
        sys.exit(f"DETENIDO: el fondo dura {-dif:.2f} s MENOS que la locución. Probablemente se cortó algo en el "
                 "montaje: el doblaje quedaría desfasado desde ese punto. Revisa el capítulo recortado y sube su WAV "
                 "corregido (o el fondo correcto).")
    if dif > COLA_MAX:
        sys.exit(f"DETENIDO: el fondo dura {dif:.2f} s más que la locución (máximo aceptado al final: {COLA_MAX:.0f} s). "
                 "Si de verdad es una cola larga, vuelve a correr con COLA_MAX mayor.")
    if dif > 0.05:
        log(f"   el fondo es {dif:.2f} s más largo: se asume cola de cierre y se rellena la voz con silencio al final")
    pistas = sorted(salida.glob("pista_voz_*.flac"))
    if not pistas:
        sys.exit("No hay pistas de voz (pista_voz_XX.flac): corre primero los idiomas.")
    for ruta in pistas:
        idioma = ruta.stem.replace("pista_voz_", "")
        info = leer_json(ruta.with_suffix(".json"), {"escala": 1.0})
        voz, _ = sf.read(str(ruta), dtype="float32")
        voz = voz / info["escala"]
        if len(voz) > len(fondo):
            exceso = (len(voz) - len(fondo)) / SR
            if np.max(np.abs(voz[len(fondo):])) > 1e-3:
                log(f"!! {idioma}: la voz sigue sonando {exceso:.1f} s después del final del fondo (se corta).")
            voz = voz[:len(fondo)]
        pista = np.zeros(len(fondo), dtype=np.float32)
        pista[:len(voz)] = voz
        mezcla = fondo + pista[:, None]
        pico = np.max(np.abs(mezcla))
        if pico > 0.98:
            mezcla *= 0.98 / pico
        tmp = salida / f"mezcla_{idioma}.wav"
        sf.write(str(tmp), mezcla, SR, subtype="PCM_16")
        final = salida / nombre_mp3(args.video, idioma)
        ffmpeg("-i", str(tmp), "-c:a", "libmp3lame", "-b:a", "192k", str(final))
        tmp.unlink()
        log(f"OK -> {final.name}")


# ---------------------------------------------------------------- medición de tiempos por paso (para el reporte)

TIEMPOS = {}


_ACTIVOS = set()


def _medir(nombre, funcion):
    def envuelta(*a, **k):
        if nombre in _ACTIVOS:               # p. ej. Azure como respaldo dentro de edge-tts: no contar doble
            return funcion(*a, **k)
        _ACTIVOS.add(nombre)
        t = time.time()
        try:
            return funcion(*a, **k)
        finally:
            _ACTIVOS.discard(nombre)
            TIEMPOS[nombre] = TIEMPOS.get(nombre, 0) + time.time() - t
    return envuelta


transcribir = _medir("Whisper (escuchar y revisar)", transcribir)
Claude.pedir = _medir("Claude (traducir, reescribir, revisar; incluye esperas)", Claude.pedir)
Edge.generar = _medir("Voz (edge-tts / Azure)", Edge.generar)
Azure.generar = _medir("Voz (edge-tts / Azure)", Azure.generar)


def main():
    load_dotenv(Path(__file__).with_name(".env"))
    p = argparse.ArgumentParser(description="Doblaje multi-idioma (nube)")
    sub = p.add_subparsers(dest="comando", required=True)
    a = sub.add_parser("preparar")
    a.add_argument("--capitulos", required=True, help="carpeta con PREFIJO-capNN.wav y PREFIJO-capNN.txt")
    a.add_argument("--salida", required=True)
    a.add_argument("--video", required=True, help="nombre del video (ej.: video07)")
    b = sub.add_parser("idioma")
    b.add_argument("--idioma", required=True)
    b.add_argument("--salida", required=True)
    b.add_argument("--video", required=True)
    b.add_argument("--glosario", help="archivo .txt opcional con nombres propios y contexto del video")
    b.add_argument("--titulo", help="titulo.txt opcional: se traduce a meta_XX.json")
    t = sub.add_parser("titulos")
    t.add_argument("--titulo", required=True)
    t.add_argument("--idiomas", required=True)
    t.add_argument("--salida", required=True)
    t.add_argument("--glosario")
    c = sub.add_parser("mezclar")
    c.add_argument("--fondo", required=True)
    c.add_argument("--salida", required=True)
    c.add_argument("--video", required=True)
    args = p.parse_args()
    if args.comando == "idioma" and (not WHISPER_CLI.exists() or not WHISPER_MODELO.exists()):
        sys.exit(f"No encuentro whisper-cli ({WHISPER_CLI}) o el modelo ({WHISPER_MODELO})")
    {"preparar": cmd_preparar, "idioma": cmd_idioma, "titulos": cmd_titulos,
     "mezclar": cmd_mezclar}[args.comando](args)


if __name__ == "__main__":
    main()
