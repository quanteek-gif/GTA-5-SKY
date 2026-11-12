# -*- coding: utf-8 -*-
"""
GTA 5 Sky Changer — подмена текстуры неба на свою картинку.
Только для одиночной игры (single player). Не заходи с модами в GTA Online.

Что делает программа:
  1. Запоминает папку игры в config.json рядом со скриптом.
  2. Перед первой заменой копирует исходный файл неба из папки mods в папку backup.
  3. Приводит выбранную картинку к нужному размеру и сохраняет её в формате DDS.
  4. По кнопке «Вернуть оригинал» копирует файл из backup обратно.

ВАЖНО. GTA 5 не читает отдельные .dds с диска: текстуры неба лежат в файле
skydome.ytd внутри RPF-архива. Готовый DDS нужно импортировать в skydome.ytd
своей копии архива в папке mods через OpenIV (Edit mode, кнопка Replace).
Программа пишет только внутрь папки mods и не трогает оригинальные файлы игры.

Нужна библиотека Pillow версии 11.2.1 или новее:
    pip install --upgrade "pillow>=11.2.1"
"""

import errno
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import tkinter as tk
from io import BytesIO
from tkinter import filedialog, messagebox, ttk

# Pillow — единственная сторонняя библиотека.
# Если её нет, программа не падает молча, а показывает окно с подсказкой (см. main).
try:
    from PIL import Image, ImageOps, ImageTk, UnidentifiedImageError
    from PIL import __version__ as PILLOW_VERSION
except ImportError:
    Image = ImageOps = ImageTk = None
    UnidentifiedImageError = OSError
    PILLOW_VERSION = "не установлена"


# =====================================================================
#  НАСТРОЙКИ — всё, что может понадобиться поменять, собрано здесь
# =====================================================================

# Имя папки с модами внутри папки игры (её создаёт OpenIV).
MODS_DIR_NAME = "mods"

# Путь к файлу неба ВНУТРИ папки mods (относительно неё).
# Итоговый путь: <папка игры>/mods/sky_changer/starfield.dds
# Поменяй на свой, если файл неба у тебя лежит в другом месте.
SKY_FILE_REL_PATH = os.path.join("sky_changer", "starfield.dds")

# Размер итоговой текстуры в пикселях: (ширина, высота).
# Для DXT обе стороны должны быть кратны 4; надёжнее всего степени двойки.
TEXTURE_SIZE = (2048, 1024)

# Формат DDS:
#   "DXT1" (он же BC1) — сжатие без прозрачности, самый лёгкий файл;
#   "DXT3" (BC2), "DXT5" (BC3) — сжатие с альфа-каналом;
#   "RGBA" — без сжатия (A8R8G8B8), работает на любой версии Pillow.
DDS_FORMAT = "DXT1"

# Создавать ли мип-уровни (уменьшенные копии текстуры внутри DDS).
GENERATE_MIPMAPS = True

# Как подгонять картинку под TEXTURE_SIZE:
#   "stretch" — просто растянуть до нужного размера;
#   "crop"    — сохранить пропорции и обрезать лишнее по центру.
RESIZE_MODE = "stretch"

# Запасной конвертер: texconv.exe из Microsoft DirectXTex.
# Нужен, только если Pillow не умеет сжимать в выбранный формат.
# Достаточно положить texconv.exe рядом со скриптом (или рядом с .exe программы).
TEXCONV_EXE = "texconv.exe"

# Файл настроек и папка бэкапа — создаются рядом со скриптом.
CONFIG_FILE_NAME = "config.json"
BACKUP_DIR_NAME = "backup"

# Исполняемые файлы игры — по ним проверяем, что выбрана именно папка GTA 5.
GAME_EXE_NAMES = ("GTA5.exe", "GTA5_Enhanced.exe")

# Размер области предпросмотра в окне: (ширина, высота) в пикселях.
PREVIEW_SIZE = (440, 220)

# Заголовок окна.
APP_TITLE = "GTA 5 Sky Changer — только single player"

# Подсказка в окне с результатом, когда файл неба — отдельный .dds.
# Пустая строка — не показывать.
IMPORT_HINT = (
    "Напоминание: игра читает текстуры только из .ytd внутри RPF-архивов. "
    "Чтобы небо в игре изменилось, импортируй этот DDS в skydome.ytd "
    "в папке mods через OpenIV."
)


# =====================================================================
#  Служебные значения (менять не нужно)
# =====================================================================

# Другие названия тех же форматов
_FORMAT_ALIASES = {"BC1": "DXT1", "BC2": "DXT3", "BC3": "DXT5", "ARGB": "RGBA", "A8R8G8B8": "RGBA"}
# Сколько байт занимает один блок 4x4 пикселя в сжатых форматах
_BLOCK_BYTES = {"DXT1": 8, "DXT3": 16, "DXT5": 16}
# Названия форматов для texconv.exe
_TEXCONV_FORMATS = {"DXT1": "BC1_UNORM", "DXT3": "BC2_UNORM", "DXT5": "BC3_UNORM", "RGBA": "B8G8R8A8_UNORM"}

# Фильтр для уменьшения картинки (в разных версиях Pillow он лежит в разных местах)
RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS if Image is not None else None


class SkyError(Exception):
    """Ошибка с понятным текстом, который можно показать пользователю."""


class PillowCannotCompress(Exception):
    """Установленная версия Pillow не смогла сжать DDS в нужный формат."""


def get_app_dir():
    """Папка, где лежит скрипт (или .exe после сборки PyInstaller)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = get_app_dir()
CONFIG_PATH = os.path.join(APP_DIR, CONFIG_FILE_NAME)
BACKUP_DIR = os.path.join(APP_DIR, BACKUP_DIR_NAME)


def describe_os_error(exc):
    """Переводит системную ошибку в понятное объяснение на русском."""
    if isinstance(exc, PermissionError):
        return (
            "Нет доступа к файлу или папке. Закрой игру и OpenIV, проверь, что у файла "
            "не стоит «Только чтение», или запусти программу от имени администратора."
        )
    if isinstance(exc, FileNotFoundError):
        return "Файл или папка не найдены."
    if getattr(exc, "errno", None) == errno.ENOSPC:
        return "На диске закончилось место."
    return "Системная ошибка: {}".format(exc)


def remove_quietly(path):
    """Удаляет временный файл, не обращая внимания на ошибки."""
    try:
        os.remove(path)
    except OSError:
        pass


def shorten(text, limit=36):
    """Укорачивает длинное имя файла, чтобы оно не растягивало окно."""
    if len(text) <= limit:
        return text
    keep = (limit - 1) // 2
    return text[:keep] + "…" + text[-keep:]


# ---------------------------------------------------------------------
#  Настройки (config.json)
# ---------------------------------------------------------------------

def load_config():
    """Читает config.json. Если файла нет или он испорчен — возвращает пустые настройки."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data):
    """Записывает настройки в config.json."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise SkyError(
            "Не удалось сохранить настройки в файл:\n{}\n\n{}".format(CONFIG_PATH, describe_os_error(exc))
        )


# ---------------------------------------------------------------------
#  Пути
# ---------------------------------------------------------------------

def resolve_paths(game_dir):
    """
    Возвращает (папка mods, файл неба, файл бэкапа).
    Заодно проверяет, что файл неба лежит строго внутри папки mods:
    так программа не сможет задеть оригинальные файлы игры.
    """
    game_dir = os.path.abspath(game_dir)
    mods_dir = os.path.abspath(os.path.join(game_dir, MODS_DIR_NAME))
    target = os.path.abspath(os.path.join(mods_dir, SKY_FILE_REL_PATH))

    norm = os.path.normcase
    try:
        mods_in_game = os.path.commonpath([norm(game_dir), norm(mods_dir)]) == norm(game_dir)
        target_in_mods = os.path.commonpath([norm(mods_dir), norm(target)]) == norm(mods_dir)
    except ValueError:  # пути оказались на разных дисках
        mods_in_game = target_in_mods = False

    if (
        not mods_in_game
        or not target_in_mods
        or norm(mods_dir) == norm(game_dir)
        or norm(target) == norm(mods_dir)
    ):
        raise SkyError(
            "Файл неба должен находиться внутри папки mods.\n"
            "Проверь константы MODS_DIR_NAME и SKY_FILE_REL_PATH в начале скрипта."
        )

    # Бэкап повторяет путь файла внутри mods: backup/sky_changer/starfield.dds
    backup = os.path.join(BACKUP_DIR, os.path.relpath(target, mods_dir))
    return mods_dir, target, backup


def has_game_exe(game_dir):
    """Есть ли в папке исполняемый файл игры."""
    return any(os.path.isfile(os.path.join(game_dir, name)) for name in GAME_EXE_NAMES)


# ---------------------------------------------------------------------
#  Картинка
# ---------------------------------------------------------------------

def open_image(path):
    """Открывает картинку, учитывает поворот из EXIF и возвращает её в режиме RGBA."""
    try:
        with Image.open(path) as img:
            return ImageOps.exif_transpose(img).convert("RGBA")
    except FileNotFoundError:
        raise SkyError("Картинка не найдена (её переместили или удалили?):\n{}".format(path))
    except PermissionError:
        raise SkyError("Нет доступа к файлу картинки:\n{}".format(path))
    except UnidentifiedImageError:
        raise SkyError("Файл повреждён или это не картинка PNG/JPG:\n{}".format(path))
    except Image.DecompressionBombError:
        raise SkyError("Картинка слишком большая. Уменьши её в любом редакторе и попробуй снова.")
    except MemoryError:
        raise SkyError("Не хватило оперативной памяти, чтобы открыть картинку. Возьми картинку поменьше.")
    except (OSError, ValueError) as exc:
        raise SkyError("Не удалось прочитать картинку:\n{}\n\n{}".format(path, exc))


def prepare_texture(img):
    """Шаг 3: приводит картинку к размеру TEXTURE_SIZE."""
    size = (int(TEXTURE_SIZE[0]), int(TEXTURE_SIZE[1]))
    if RESIZE_MODE == "crop":
        return ImageOps.fit(img, size, RESAMPLE, centering=(0.5, 0.5))
    return img.resize(size, RESAMPLE)


# ---------------------------------------------------------------------
#  Конвертация в DDS
# ---------------------------------------------------------------------

def normalize_format(name):
    """Приводит название формата к одному из: DXT1, DXT3, DXT5, RGBA."""
    fmt = str(name).strip().upper()
    fmt = _FORMAT_ALIASES.get(fmt, fmt)
    if fmt != "RGBA" and fmt not in _BLOCK_BYTES:
        raise SkyError(
            "Неизвестный формат DDS: «{}».\n"
            "В константе DDS_FORMAT можно указать DXT1 (BC1), DXT3 (BC2), DXT5 (BC3) или RGBA.".format(name)
        )
    return fmt


def check_texture_size(fmt):
    """Проверяет константу TEXTURE_SIZE и возвращает (ширина, высота)."""
    try:
        width, height = int(TEXTURE_SIZE[0]), int(TEXTURE_SIZE[1])
    except (TypeError, ValueError, IndexError):
        raise SkyError("Константа TEXTURE_SIZE должна иметь вид (ширина, высота), например (2048, 1024).")
    if not (4 <= width <= 16384 and 4 <= height <= 16384):
        raise SkyError("Размер текстуры должен быть от 4 до 16384 пикселей по каждой стороне.")
    if fmt != "RGBA" and (width % 4 or height % 4):
        raise SkyError(
            "Для сжатых форматов (DXT) ширина и высота текстуры должны быть кратны 4.\n"
            "Сейчас TEXTURE_SIZE = {}x{}.".format(width, height)
        )
    return width, height


def mip_sizes(width, height):
    """
    Размеры мип-уровней, начиная с полного размера.
    Цепочка продолжается, пока обе стороны делятся пополам и остаются кратными 4
    (блоки DXT — это квадраты 4x4, уровни мельче игре не нужны).
    """
    sizes = [(width, height)]
    while GENERATE_MIPMAPS and width % 8 == 0 and height % 8 == 0:
        width, height = width // 2, height // 2
        sizes.append((width, height))
    return sizes


def make_dds_header(width, height, mip_count, fmt, top_level_bytes):
    """Собирает стандартный заголовок DDS-файла (128 байт)."""
    DDSD_CAPS, DDSD_HEIGHT, DDSD_WIDTH, DDSD_PITCH = 0x1, 0x2, 0x4, 0x8
    DDSD_PIXELFORMAT, DDSD_MIPMAPCOUNT, DDSD_LINEARSIZE = 0x1000, 0x20000, 0x80000
    DDPF_ALPHAPIXELS, DDPF_FOURCC, DDPF_RGB = 0x1, 0x4, 0x40
    DDSCAPS_COMPLEX, DDSCAPS_TEXTURE, DDSCAPS_MIPMAP = 0x8, 0x1000, 0x400000

    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT
    caps = DDSCAPS_TEXTURE
    if mip_count > 1:
        flags |= DDSD_MIPMAPCOUNT
        caps |= DDSCAPS_COMPLEX | DDSCAPS_MIPMAP

    if fmt == "RGBA":
        # Несжатый A8R8G8B8: 32 бита на пиксель и маски каналов R, G, B, A
        flags |= DDSD_PITCH
        pitch_or_size = width * 4
        pixel_format = struct.pack(
            "<II4sIIIII", 32, DDPF_RGB | DDPF_ALPHAPIXELS, b"\x00\x00\x00\x00", 32,
            0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000,
        )
    else:
        # Сжатый формат задаётся четырёхбуквенным кодом: DXT1, DXT3 или DXT5
        flags |= DDSD_LINEARSIZE
        pitch_or_size = top_level_bytes
        pixel_format = struct.pack("<II4sIIIII", 32, DDPF_FOURCC, fmt.encode("ascii"), 0, 0, 0, 0, 0)

    header = struct.pack("<4sIIIIIII", b"DDS ", 124, flags, height, width, pitch_or_size, 0, mip_count)
    header += b"\x00" * 44                               # 11 зарезервированных полей
    header += pixel_format                               # описание формата пикселей (32 байта)
    header += struct.pack("<IIIII", caps, 0, 0, 0, 0)    # возможности текстуры
    return header


def encode_level(img, fmt):
    """Кодирует один мип-уровень и возвращает данные пикселей без заголовка."""
    if fmt == "RGBA":
        # В несжатом DDS байты идут в порядке B, G, R, A
        return img.convert("RGBA").tobytes("raw", "BGRA")

    # DXT1 не хранит прозрачность — отдаём RGB; DXT3 и DXT5 — RGBA
    source = img.convert("RGB" if fmt == "DXT1" else "RGBA")
    buf = BytesIO()
    source.save(buf, format="DDS", pixel_format=fmt)
    data = buf.getvalue()

    # Старые версии Pillow молча игнорируют pixel_format и пишут несжатый файл,
    # поэтому проверяем заголовок и размер того, что получилось.
    if data[:4] != b"DDS " or data[84:88] != fmt.encode("ascii"):
        raise PillowCannotCompress("эта версия Pillow не умеет сохранять сжатый DDS")
    expected = (img.width // 4) * (img.height // 4) * _BLOCK_BYTES[fmt]
    if len(data) != 128 + expected:
        raise PillowCannotCompress("Pillow вернула данные неожиданного размера")
    return data[128:]


def build_dds_with_pillow(img, fmt, sizes):
    """Собирает DDS целиком: наш заголовок + все мип-уровни, сжатые кодировщиком Pillow."""
    levels = []
    for size in sizes:
        level = img if size == img.size else img.resize(size, RESAMPLE)
        levels.append(encode_level(level, fmt))
    header = make_dds_header(img.width, img.height, len(levels), fmt, len(levels[0]))
    return header + b"".join(levels)


def find_texconv():
    """Ищет texconv.exe: рядом со скриптом, внутри .exe-сборки, затем в PATH."""
    if os.path.isabs(TEXCONV_EXE):
        candidates = [TEXCONV_EXE]
    else:
        candidates = [os.path.join(APP_DIR, TEXCONV_EXE)]
        bundle_dir = getattr(sys, "_MEIPASS", None)  # временная папка PyInstaller
        if bundle_dir:
            candidates.append(os.path.join(bundle_dir, TEXCONV_EXE))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return shutil.which(os.path.basename(TEXCONV_EXE))


def build_dds_with_texconv(img, fmt, mip_count, texconv_path):
    """Запасной способ: сжатие через texconv.exe (Microsoft DirectXTex)."""
    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="sky_changer_")
        src_png = os.path.join(tmp_dir, "sky.png")
        out_dds = os.path.join(tmp_dir, "sky.dds")
        img.convert("RGB" if fmt == "DXT1" else "RGBA").save(src_png, format="PNG")

        command = [
            texconv_path, "-nologo", "-y",
            "-f", _TEXCONV_FORMATS[fmt],   # формат сжатия
            "-m", str(mip_count),          # число мип-уровней
            "-o", tmp_dir,                 # куда положить результат
            src_png,
        ]
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # без чёрного окна консоли
        )
        if result.returncode != 0 or not os.path.isfile(out_dds):
            output = result.stdout.decode("utf-8", errors="replace").strip()
            raise SkyError(
                "texconv.exe не смог сконвертировать картинку:\n{}".format(output[-600:] or "нет вывода")
            )
        with open(out_dds, "rb") as f:
            return f.read()
    except subprocess.TimeoutExpired:
        raise SkyError("texconv.exe не ответил за 3 минуты. Конвертация прервана.")
    except OSError as exc:
        raise SkyError("Не удалось выполнить конвертацию через texconv.exe.\n{}".format(describe_os_error(exc)))
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def build_dds(img, fmt):
    """
    Шаг 4: превращает картинку в байты готового DDS-файла.
    Возвращает (данные, число мип-уровней, название инструмента).
    Сначала пробует Pillow; если она не умеет сжимать — texconv.exe.
    """
    sizes = mip_sizes(img.width, img.height)
    try:
        return build_dds_with_pillow(img, fmt, sizes), len(sizes), "Pillow"
    except MemoryError:
        raise
    except Exception as exc:  # Pillow не справилась — причину покажем, если запасного пути тоже нет
        reason = str(exc) or type(exc).__name__

    texconv_path = find_texconv()
    if texconv_path:
        return build_dds_with_texconv(img, fmt, len(sizes), texconv_path), len(sizes), "texconv.exe"

    raise SkyError(
        "Не удалось сжать текстуру в формат {fmt} средствами Pillow ({ver}).\n"
        "Причина: {reason}\n\n"
        "Что можно сделать:\n"
        "1) Обновить Pillow (нужна версия 11.2.1 или новее):\n"
        "   pip install --upgrade pillow\n"
        "2) Положить texconv.exe (Microsoft DirectXTex) рядом с программой.\n"
        '3) Поставить DDS_FORMAT = "RGBA" в начале скрипта — несжатый DDS '
        "пишется без кодировщика.".format(fmt=fmt, ver=PILLOW_VERSION, reason=reason)
    )


# ---------------------------------------------------------------------
#  Файлы: бэкап, запись, восстановление
# ---------------------------------------------------------------------

def copy_file_safely(src, dst):
    """Копирует файл через временный: при сбое на месте назначения не останется «половинки»."""
    tmp = dst + ".tmp"
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except OSError:
        remove_quietly(tmp)
        raise


def write_file_safely(path, data):
    """Записывает файл через временный: при сбое старый файл останется целым."""
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        remove_quietly(tmp)
        raise


def make_backup(target, backup):
    """
    Шаг 2: бэкап. Существующий бэкап никогда не перезаписывается.
    Возвращает "exists" (уже был), "created" (создан) или "no_original" (сохранять нечего).
    """
    if os.path.exists(backup):
        return "exists"
    if not os.path.isfile(target):
        return "no_original"
    try:
        copy_file_safely(target, backup)
    except OSError as exc:
        raise SkyError(
            "Не удалось сделать бэкап оригинального файла, поэтому замена отменена.\n\n"
            "Откуда: {}\nКуда: {}\n\n{}".format(target, backup, describe_os_error(exc))
        )
    return "created"


# ---------------------------------------------------------------------
#  Окно программы
# ---------------------------------------------------------------------

class SkyChangerApp(tk.Tk):
    """Главное окно программы."""

    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.resizable(False, False)

        self.config_data = load_config()
        self.image_path = None       # путь к выбранной картинке
        self.preview_photo = None    # ссылку на картинку предпросмотра нужно хранить, иначе она исчезнет
        self._busy = False           # True, пока идёт операция с файлами

        # Масштаб интерфейса Windows (100 % = 1.0), чтобы предпросмотр не был мелким на 4K-экране
        try:
            scale = self.winfo_fpixels("1i") / 96.0
        except tk.TclError:
            scale = 1.0
        scale = min(max(scale, 1.0), 3.0)
        self.preview_size = (int(PREVIEW_SIZE[0] * scale), int(PREVIEW_SIZE[1] * scale))

        # Путь к игре подставляем из config.json
        saved_dir = str(self.config_data.get("game_dir", "") or "")
        if saved_dir:
            start_status = "Папка игры подставлена из config.json. Выбери картинку и нажми «Применить»."
        else:
            start_status = "Выбери папку GTA 5 и картинку, затем нажми «Применить»."

        self.game_dir_var = tk.StringVar(master=self, value=saved_dir)
        self.image_name_var = tk.StringVar(master=self, value="Картинка: не выбрана")
        self.image_info_var = tk.StringVar(master=self, value="")
        self.status_var = tk.StringVar(master=self, value=start_status)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self):
        """Создаёт все элементы окна."""
        main = ttk.Frame(self, padding=14)
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)

        # 1. Папка игры: подпись, поле и кнопка
        ttk.Label(main, text="Папка GTA 5:").grid(row=0, column=0, columnspan=2, sticky="w")
        self.game_dir_entry = ttk.Entry(main, textvariable=self.game_dir_var, width=52)
        self.game_dir_entry.grid(row=1, column=0, sticky="ew", pady=(2, 12))
        self.btn_game_dir = ttk.Button(main, text="Выбрать папку GTA 5", command=self.choose_game_dir)
        self.btn_game_dir.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(2, 12))

        # 2. Картинка: имя файла, кнопка и предпросмотр
        ttk.Label(main, textvariable=self.image_name_var).grid(row=2, column=0, sticky="w")
        self.btn_image = ttk.Button(main, text="Выбрать картинку", command=self.choose_image)
        self.btn_image.grid(row=2, column=1, sticky="ew", padx=(8, 0))

        self.preview_frame = tk.Frame(
            main, width=self.preview_size[0], height=self.preview_size[1],
            bg="#1e1e1e", highlightthickness=1, highlightbackground="#8a8a8a",
        )
        self.preview_frame.grid(row=3, column=0, columnspan=2, pady=(10, 4))
        self.preview_label = tk.Label(
            self.preview_frame, text="Здесь появится предпросмотр", bg="#1e1e1e", fg="#bdbdbd",
        )
        self.preview_label.place(relx=0.5, rely=0.5, anchor="center")

        self.image_info_label = ttk.Label(main, textvariable=self.image_info_var, foreground="#555555")
        self.image_info_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 10))

        # 3 и 4. Кнопки «Применить» и «Вернуть оригинал»
        actions = ttk.Frame(main)
        actions.grid(row=5, column=0, columnspan=2, sticky="ew")
        actions.columnconfigure(0, weight=1, uniform="actions")
        actions.columnconfigure(1, weight=1, uniform="actions")
        self.btn_apply = ttk.Button(actions, text="Применить", command=self.on_apply)
        self.btn_apply.grid(row=0, column=0, sticky="ew", padx=(0, 4), ipady=5)
        self.btn_restore = ttk.Button(actions, text="Вернуть оригинал", command=self.on_restore)
        self.btn_restore.grid(row=0, column=1, sticky="ew", padx=(4, 0), ipady=5)

        ttk.Label(
            main, text="Только для одиночной игры. Не заходи с модами в GTA Online.", foreground="#555555",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # 5. Строка статуса внизу окна
        self.status_label = ttk.Label(
            self, textvariable=self.status_var, relief="sunken", anchor="w", padding=(8, 4),
        )
        self.status_label.grid(row=1, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)

        # Длинные тексты переносим по ширине окна, чтобы оно не «разъезжалось»
        self.update_idletasks()
        wrap = max(main.winfo_reqwidth() - 30, 200)
        self.status_label.configure(wraplength=wrap)
        self.image_info_label.configure(wraplength=wrap)

        # Ставим окно примерно по центру экрана
        x = max((self.winfo_screenwidth() - self.winfo_reqwidth()) // 2, 0)
        y = max((self.winfo_screenheight() - self.winfo_reqheight()) // 3, 0)
        self.geometry("+{}+{}".format(x, y))

    # ----- вспомогательные методы -----

    def set_status(self, text):
        """Показывает текст в строке статуса и сразу перерисовывает окно."""
        self.status_var.set(text)
        self.update_idletasks()

    def show_error(self, text):
        """Показывает ошибку и в окне сообщения, и в строке статуса."""
        first_line = (text.splitlines() or [""])[0]
        self.set_status("Ошибка: " + first_line)
        messagebox.showerror("Ошибка", text, parent=self)

    def set_busy(self, busy):
        """Выключает кнопки на время работы, чтобы операцию нельзя было запустить дважды."""
        if busy:
            self._busy = True
        else:
            self.update()  # «съедаем» клики, сделанные во время работы, пока кнопки ещё выключены
            self._busy = False
        state = "disabled" if busy else "normal"
        for button in (self.btn_game_dir, self.btn_image, self.btn_apply, self.btn_restore):
            button.configure(state=state)
        self.configure(cursor="watch" if busy else "")
        self.update_idletasks()

    def read_game_dir(self):
        """Берёт путь к игре из поля и проверяет, что такая папка есть. При ошибке возвращает None."""
        game_dir = self.game_dir_var.get().strip().strip('"').strip()
        if not game_dir:
            self.show_error("Сначала выбери папку GTA 5.")
            return None
        if not os.path.isdir(game_dir):
            self.show_error("Папка игры не найдена. Проверь путь:\n{}".format(game_dir))
            return None
        return os.path.normpath(game_dir)

    def remember_game_dir(self, game_dir, quiet=False):
        """Сохраняет путь к игре в config.json."""
        self.config_data["game_dir"] = game_dir
        try:
            save_config(self.config_data)
        except SkyError as exc:
            if not quiet:
                messagebox.showwarning("Настройки не сохранены", str(exc), parent=self)

    # ----- кнопки -----

    def choose_game_dir(self):
        """Кнопка «Выбрать папку GTA 5»."""
        options = {"parent": self, "title": "Выбери папку, в которой установлена GTA 5", "mustexist": True}
        current = self.game_dir_var.get().strip()
        if os.path.isdir(current):
            options["initialdir"] = current
        path = filedialog.askdirectory(**options)
        if not path:
            return  # нажали «Отмена»
        path = os.path.normpath(path)
        self.game_dir_var.set(path)
        self.remember_game_dir(path)
        if has_game_exe(path):
            self.set_status("Папка игры сохранена: {}".format(path))
        else:
            self.set_status(
                "Папка сохранена, но {} в ней не найден — проверь, что это папка игры.".format(GAME_EXE_NAMES[0])
            )

    def choose_image(self):
        """Кнопка «Выбрать картинку»: запоминает файл и показывает предпросмотр."""
        path = filedialog.askopenfilename(
            parent=self,
            title="Выбери картинку для неба",
            filetypes=[("Картинки PNG, JPG, JPEG", ("*.png", "*.jpg", "*.jpeg"))],
        )
        if not path:
            return  # нажали «Отмена»
        path = os.path.normpath(path)
        if not path.lower().endswith((".png", ".jpg", ".jpeg")):
            self.show_error("Подходят только картинки PNG, JPG и JPEG.")
            return

        self.set_status("Открываю картинку…")
        try:
            img = open_image(path)
            width, height = img.size
            img.thumbnail(self.preview_size, RESAMPLE)   # уменьшаем копию в памяти для предпросмотра
            photo = ImageTk.PhotoImage(img, master=self)
        except SkyError as exc:
            self.show_error(str(exc))
            return
        except Exception as exc:  # страховка: любая другая ошибка при показе картинки
            self.show_error("Не удалось показать картинку: {}".format(exc))
            return

        self.image_path = path
        self.preview_photo = photo
        self.preview_label.configure(image=photo, text="")
        self.image_name_var.set("Картинка: " + shorten(os.path.basename(path)))

        tex_w, tex_h = int(TEXTURE_SIZE[0]), int(TEXTURE_SIZE[1])
        info = "Исходный размер: {}×{}  →  текстура: {}×{}".format(width, height, tex_w, tex_h)
        if abs(width / height - tex_w / tex_h) > 0.01:
            if RESIZE_MODE == "crop":
                info += " (лишнее по краям будет обрезано)"
            else:
                info += " (пропорции другие — картинка растянется)"
        self.image_info_var.set(info)
        self.set_status("Картинка выбрана. Нажми «Применить».")

    def on_apply(self):
        """Кнопка «Применить»: бэкап, размер, DDS, сохранение в папку mods."""
        # 1. Проверяем папку игры, картинку и настройки
        game_dir = self.read_game_dir()
        if game_dir is None:
            return
        if not self.image_path:
            self.show_error("Сначала выбери картинку (PNG, JPG или JPEG).")
            return
        try:
            fmt = normalize_format(DDS_FORMAT)
            width, height = check_texture_size(fmt)
            mods_dir, target, backup = resolve_paths(game_dir)
        except SkyError as exc:
            self.show_error(str(exc))
            return

        if not has_game_exe(game_dir):
            go_on = messagebox.askyesno(
                "Проверь папку",
                "В выбранной папке нет {}.\nПохоже, это не папка GTA 5. Всё равно продолжить?".format(
                    GAME_EXE_NAMES[0]
                ),
                parent=self,
            )
            if not go_on:
                self.set_status("Отменено. Выбери папку, в которой лежит {}.".format(GAME_EXE_NAMES[0]))
                return

        if not os.path.isdir(mods_dir):
            create = messagebox.askyesno(
                "Папка mods не найдена",
                "В папке игры нет папки «{}».\n"
                "Обычно её создаёт OpenIV (Tools → ASI Manager). Без OpenIV.asi игра "
                "не загружает моды из этой папки.\n\n"
                "Создать папку сейчас и продолжить?".format(MODS_DIR_NAME),
                parent=self,
            )
            if not create:
                self.set_status("Отменено: папки mods нет.")
                return

        self.remember_game_dir(game_dir)

        self.set_busy(True)
        error = None
        try:
            # 2. Бэкап оригинала (существующий бэкап не трогаем)
            self.set_status("Шаг 1 из 4: проверяю бэкап…")
            backup_state = make_backup(target, backup)

            # 3. Меняем размер картинки
            self.set_status("Шаг 2 из 4: меняю размер картинки до {}×{}…".format(width, height))
            texture = prepare_texture(open_image(self.image_path))

            # 4. Конвертируем в DDS и сохраняем в папку mods
            self.set_status("Шаг 3 из 4: конвертирую в DDS ({})…".format(fmt))
            data, mip_count, tool = build_dds(texture, fmt)

            self.set_status("Шаг 4 из 4: сохраняю файл в папку mods…")
            try:
                write_file_safely(target, data)
            except OSError as exc:
                raise SkyError("Не удалось сохранить файл неба:\n{}\n\n{}".format(target, describe_os_error(exc)))
        except SkyError as exc:
            error = str(exc)
        except MemoryError:
            error = "Не хватило оперативной памяти. Закрой лишние программы или возьми картинку поменьше."
        except Exception as exc:  # страховка от любых непредвиденных ошибок
            error = "Непредвиденная ошибка: {}: {}".format(type(exc).__name__, exc)
        finally:
            self.set_busy(False)

        if error:
            self.show_error(error)
            return

        # 5. Сообщаем о результате
        backup_notes = {
            "created": "Оригинал сохранён в бэкап:\n{}".format(backup),
            "exists": "Бэкап уже был, он не перезаписывался:\n{}".format(backup),
            "no_original": "Бэкап не создавался: до замены файла неба в папке mods не было.",
        }
        self.set_status(
            "Готово, перезапусти игру. Файл: {} ({}, {}×{}, мип-уровней: {}, через {}).".format(
                target, fmt, width, height, mip_count, tool
            )
        )
        message = "Готово, перезапусти игру.\n\nФайл сохранён:\n{}\n\n{}".format(target, backup_notes[backup_state])
        if IMPORT_HINT and target.lower().endswith(".dds"):
            message += "\n\n" + IMPORT_HINT
        messagebox.showinfo("Готово", message, parent=self)

    def on_restore(self):
        """Кнопка «Вернуть оригинал»: копирует файл из backup обратно в папку mods."""
        game_dir = self.read_game_dir()
        if game_dir is None:
            return
        try:
            mods_dir, target, backup = resolve_paths(game_dir)
        except SkyError as exc:
            self.show_error(str(exc))
            return

        # Бэкапа нет — сообщаем об этом и ничего не трогаем
        if not os.path.isfile(backup):
            self.set_status("Бэкап не найден — возвращать нечего.")
            messagebox.showwarning(
                "Бэкапа нет",
                "Бэкап не найден:\n{}\n\n"
                "Он создаётся при первом нажатии «Применить», если в папке mods "
                "уже лежал файл неба.".format(backup),
                parent=self,
            )
            return
        if not os.path.isdir(mods_dir):
            self.show_error(
                "В папке игры нет папки «{}» — возвращать файл некуда.\n"
                "Проверь, что выбрана правильная папка GTA 5.".format(MODS_DIR_NAME)
            )
            return

        self.remember_game_dir(game_dir)

        self.set_busy(True)
        error = None
        try:
            self.set_status("Возвращаю оригинальный файл из бэкапа…")
            copy_file_safely(backup, target)
        except OSError as exc:
            error = "Не удалось вернуть оригинал:\n{}\n\n{}".format(target, describe_os_error(exc))
        except Exception as exc:  # страховка от любых непредвиденных ошибок
            error = "Непредвиденная ошибка: {}: {}".format(type(exc).__name__, exc)
        finally:
            self.set_busy(False)

        if error:
            self.show_error(error)
            return

        self.set_status("Оригинал возвращён, перезапусти игру. Файл: {}".format(target))
        message = "Оригинал возвращён, перезапусти игру.\n\n{}".format(target)
        if IMPORT_HINT and target.lower().endswith(".dds"):
            message += "\n\n" + IMPORT_HINT
        messagebox.showinfo("Готово", message, parent=self)

    def on_close(self):
        """Закрытие окна: запоминаем путь, если его вписали в поле вручную."""
        if self._busy:
            return  # идёт операция с файлами — дождёмся её окончания
        game_dir = self.game_dir_var.get().strip().strip('"').strip()
        if game_dir and os.path.isdir(game_dir):
            self.remember_game_dir(os.path.normpath(game_dir), quiet=True)
        self.destroy()

    def report_callback_exception(self, exc_type, exc_value, exc_traceback):
        """Непойманные ошибки в обработчиках кнопок показываем пользователю, а не теряем."""
        try:
            self.show_error("Непредвиденная ошибка: {}: {}".format(exc_type.__name__, exc_value))
        except tk.TclError:
            pass


# ---------------------------------------------------------------------
#  Запуск
# ---------------------------------------------------------------------

def enable_sharp_text():
    """Чёткий текст на экранах с масштабом больше 100 % (Windows 8.1 и новее)."""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass  # не критично: окно просто будет чуть размытым


def main():
    enable_sharp_text()

    if Image is None:
        # Pillow не установлена — показываем подсказку и выходим
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Не установлена библиотека Pillow",
            "Для работы программы нужна библиотека Pillow.\n\n"
            "Открой командную строку и выполни:\n"
            'pip install --upgrade "pillow>=11.2.1"',
        )
        root.destroy()
        return 1

    app = SkyChangerApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
