#!/usr/bin/env python3

import os
import threading
import traceback
import sys
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor
from os import path
from typing import Literal

import pylibcue
from mutagen.wave import WAVE
from mutagen.id3 import TIT2, TPE1, TALB, TRCK, TPOS
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk
import struct


def msf2seconds(msf: tuple[int, int, int], ndigits: int = 2) -> float:
    return round(msf[0] * 60 + msf[1] + msf[2] / 75, ndigits)


def _ffmpeg_run(cmd: list[str]) -> int:
    cmd_base = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    full_cmd = cmd_base + cmd
    # 在 Windows 上避免弹出控制台窗口（ffmpeg 是控制台程序）
    run_kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    res = subprocess.run(full_cmd, **run_kwargs)
    return res.returncode


def _add_metadata(cmd: list[str], k: str, v: str | int | None) -> None:
    if v is not None:
        cmd.extend(("-metadata", f"{k}={v}"))


def _make_subchunk(tag: str, text: str | None, cue_encoding: str = "utf-8") -> bytes:
    if not text:
        return b""
    data = text.encode(cue_encoding) + b"\x00"
    size = len(data)
    pad = b"\x00" if size % 2 == 1 else b""
    return tag.encode("ascii") + struct.pack("<I", size) + data + pad


def _write_wav_riff(wav_path: str, title: str | None, artist: str | None, album: str | None, cue_encoding: str = "utf-8", track: int | None = None, disc: int | None = None) -> None:
    # 如果没有任何元数据要写，直接返回
    if not any((title, artist, album)):
        return

    with open(wav_path, "rb") as f:
        data = f.read()

    if not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
        raise ValueError("Not a valid WAV file")

    # 解析并移除已有的 LIST/INFO 块
    pos = 12  # RIFF header (12 bytes)
    kept_chunks: list[bytes] = []
    while pos + 8 <= len(data):
        cid = data[pos : pos + 4]
        csz = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        cdata_start = pos + 8
        cdata_end = cdata_start + csz
        if cdata_end > len(data):
            # 文件被截断，停止解析，保留剩余数据原样
            kept_chunks.append(data[pos:])
            break
        chunk_bytes = data[pos : cdata_end]
        pad = b"\x00" if csz % 2 == 1 else b""
        # 如果是 LIST 且类型为 INFO，则跳过（删除旧的 INFO）
        if cid == b"LIST" and data[cdata_start : cdata_start + 4] == b"INFO":
            # skip this chunk (and its padding)
            pass
        else:
            kept_chunks.append(chunk_bytes + pad)
        pos = cdata_end + (1 if csz % 2 == 1 else 0)

    # 构造新的 LIST/INFO 块
    parts = []
    parts.append(_make_subchunk("INAM", title, cue_encoding))  # track title
    parts.append(_make_subchunk("IART", artist, cue_encoding))  # artist
    parts.append(_make_subchunk("IPRD", album, cue_encoding))  # album
    # 写入轨号到 LIST/INFO（常用标签为 ITRK）
    if track is not None:
        parts.append(_make_subchunk("ITRK", str(track), cue_encoding))
    # 写入盘号到 LIST/INFO（使用 IDIS 标签）
    if disc is not None:
        parts.append(_make_subchunk("IDIS", str(disc), cue_encoding))
    info_body = b"INFO" + b"".join(p for p in parts if p)
    if len(info_body) == 4:
        # 没有实际子块（只有 "INFO"），无需写入
        new_list_chunk = b""
    else:
        list_size = len(info_body)
        list_chunk = b"LIST" + struct.pack("<I", list_size) + info_body
        if list_size % 2 == 1:
            list_chunk += b"\x00"
        new_list_chunk = list_chunk

    # 重新组装 RIFF 文件：RIFF + size + WAVE + chunks + new LIST
    body = b"WAVE" + b"".join(kept_chunks) + new_list_chunk
    new_riff_size = len(body)
    new_data = b"RIFF" + struct.pack("<I", new_riff_size) + body

    tmp_path = wav_path + ".meta.tmp"
    with open(tmp_path, "wb") as f:
        f.write(new_data)
    try:
        os.replace(tmp_path, wav_path)
    except Exception:
        # 尝试清理临时文件但不要抛出以中断其他写入
        traceback.print_exc()
        if path.exists(tmp_path):
            os.remove(tmp_path)

def split_cue(
    cue_file: os.PathLike[str] | str,
    *,
    audio_file: os.PathLike[str] | str | None = None,
    output_dir: os.PathLike[str] | str = ".",
    format: Literal["wav", "mp3", "flac"] = "flac",
    cue_encoding: str = "utf-8",
    overwrite: bool = False,
    no_metadata: bool = False,
    jobs: int = os.cpu_count() or 1,
    track_offset: int = 0,
    write_disc: bool = False,
    disc_number: int | None = None,
) -> bool:
    """cuesplit main function.

    :return: True if all tracks are processed successfully, otherwise False.
    """
    cmd = ["-y" if overwrite else "-n"]

    match format:
        case "wav":
            cmd.extend(("-c", "copy", "-f", "wav"))
        case "mp3":
            cmd.extend(("-c:a", "libmp3lame", "-b:a", "320k", "-id3v2_version", "3"))
        case "flac":
            cmd.extend(("-c:a", "flac", "-compression_level", "8"))

    cd = pylibcue.Cd.from_file(cue_file, encoding=cue_encoding)

    if format != "wav" and not no_metadata:
        _add_metadata(cmd, "album_artist", cd.cdtext.performer)
        _add_metadata(cmd, "album", cd.cdtext.title)
        _add_metadata(cmd, "date", cd.rem.date)
        # If user requested a disc number globally, add album-level disc metadata
        if write_disc and disc_number is not None:
            _add_metadata(cmd, "disc", disc_number)

    jobs_pool = ThreadPoolExecutor(max_workers=1 if format == "wav" else jobs)
    futures: list[Future[int]] = []
    pending_wav: list[tuple[Future[int], str, str | None, str | None, str | None, int, int | None]] = []

    for i in range(len(cd)):
        tr = cd[i]
        if tr.start is None:
            raise ValueError(
                f"Cannot find start time for Track {tr.track_number:02d} in cue file"
            )
        if audio_file is not None:
            input_file = os.fspath(audio_file)
        elif tr.filename is not None:
            input_file = path.join(path.dirname(cue_file), tr.filename)
        else:
            raise FileNotFoundError(f"Cannot find audio file for Track {tr.track_number:02d}")
        if not path.exists(input_file):
            raise FileNotFoundError(
                f"Input audio file {input_file} for Track {tr.track_number:02d} does not exist"
            )

        tr_cmd = ["-i", input_file]
        tr_cmd.extend(cmd)

        tr_cmd.extend(("-ss", f"{msf2seconds(tr.start):.2f}"))
        if tr.length:
            tr_cmd.extend(("-t", f"{msf2seconds(tr.length):.2f}"))

        if format != "wav" and not no_metadata:
            _add_metadata(tr_cmd, "title", tr.cdtext.title)
            _add_metadata(tr_cmd, "artist", tr.cdtext.performer or cd.cdtext.performer)
            _add_metadata(tr_cmd, "composer", tr.cdtext.composer or cd.cdtext.composer)
            # Apply track_offset to metadata track number
            try:
                md_track = (tr.track_number or 0) + int(track_offset)
            except Exception:
                md_track = tr.track_number
            _add_metadata(tr_cmd, "track", md_track)
            _add_metadata(tr_cmd, "genre", tr.cdtext.genre or cd.cdtext.genre)
            # per-track disc metadata (if requested)
            if write_disc and disc_number is not None:
                _add_metadata(tr_cmd, "disc", disc_number)

        # Compute displayed track number applying offset
        try:
            display_track = (tr.track_number or 0) + int(track_offset)
        except Exception:
            display_track = tr.track_number

        output_path = path.join(
            output_dir,
            f"{path.splitext(path.basename(input_file))[0]}_{display_track:02d}.{format}"
            if no_metadata
            else f"{display_track:02d} - {tr.cdtext.title or 'Unknown'}.{format}",
        )
        tr_cmd.append(output_path)
        fut = jobs_pool.submit(_ffmpeg_run, tr_cmd)
        futures.append(fut)

        # 延后写 WAV 标签：把未来需要写标签的文件与元数据记录下来，
        # 等待所有 ffmpeg 任务完成后再根据任务结果写入，避免竞态。
        if format == "wav" and not no_metadata:
            # Store applied track number for later metadata writing
            try:
                applied_track = (tr.track_number or 0) + int(track_offset)
            except Exception:
                applied_track = tr.track_number
            pending_wav.append((fut, output_path, cd.cdtext.performer, cd.cdtext.title, tr.cdtext.title, applied_track, disc_number if write_disc else None))
    jobs_pool.shutdown(wait=True)

    # 所有 ffmpeg 任务完成后，根据每个任务的退出码决定是否写 WAV RIFF/INFO标签（失败的任务不写，成功的任务写）
    for fut, out_path, artist, album, title, track, disc in pending_wav:
        try:
            if fut.result() == 0:
                # 写入 WAV RIFF/INFO 标签：借助 ffmpeg 将文件重写到临时文件并添加元数据，随后替换原文件
                try:
                    # 执行写入（每个成功的 wav 任务）
                    # 先改RIFF/INFO块（包含轨号和可选盘号）
                    _write_wav_riff(out_path, title, artist, album, cue_encoding, track, disc)
                    # 再写ID3块（兼容性更好）
                    w = WAVE(out_path)
                    if w.tags is None:
                        w.add_tags()
                    if title is not None:
                        w.tags.add(TIT2(encoding=3, text=title))
                    if artist is not None:
                        w.tags.add(TPE1(encoding=3, text=artist))
                    if album is not None:
                        w.tags.add(TALB(encoding=3, text=album))
                    if track is not None:
                        w.tags.add(TRCK(encoding=3, text=str(track)))
                    if disc is not None:
                        w.tags.add(TPOS(encoding=3, text=str(disc)))
                    w.save()
                except Exception:
                    # 确保任何异常不会中断其他标签写入
                    traceback.print_exc()
        except Exception:
            # 任务本身失败，跳过写标签
            traceback.print_exc()
    return all(f.result() == 0 for f in futures)


class App(tk.Tk):
    def __init__(self, split_func):
        super().__init__()
        self.title("Cue Splitter GUI")
        self.geometry("720x420")
        self.split_func = split_func
        frm = tk.Frame(self)
        frm.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # Cue file
        tk.Label(frm, text="CUE 文件:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.cue_entry = tk.Entry(frm, width=60)
        self.cue_entry.grid(row=0, column=1, sticky="we", padx=4, pady=4)
        tk.Button(frm, text="浏览", command=self.browse_cue).grid(row=0, column=2, padx=4, pady=4)

        # Audio file (optional)
        tk.Label(frm, text="音频文件 (可选):").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self.audio_entry = tk.Entry(frm, width=60)
        self.audio_entry.grid(row=1, column=1, sticky="we", padx=4, pady=4)
        tk.Button(frm, text="浏览", command=self.browse_audio).grid(row=1, column=2, padx=4, pady=4)

        # Output dir
        tk.Label(frm, text="输出目录:").grid(row=2, column=0, sticky="w", padx=4, pady=4)
        self.out_entry = tk.Entry(frm, width=60)
        self.out_entry.grid(row=2, column=1, sticky="we", padx=4, pady=4)
        tk.Button(frm, text="选择", command=self.browse_output).grid(row=2, column=2, padx=4, pady=4)

        # Format
        tk.Label(frm, text="格式:").grid(row=3, column=0, sticky="w", padx=4, pady=4)
        self.format_var = tk.StringVar(value="flac")
        self.format_box = ttk.Combobox(
            frm, textvariable=self.format_var, values=("wav", "mp3", "flac"), state="readonly", width=10
        )
        self.format_box.grid(row=3, column=1, sticky="w", padx=4, pady=4)

        # Cue encoding
        tk.Label(frm, text="CUE 编码:").grid(row=4, column=0, sticky="w", padx=4, pady=4)
        self.encoding_entry = tk.Entry(frm, width=20)
        self.encoding_entry.insert(0, "utf-8")
        self.encoding_entry.grid(row=4, column=1, sticky="w", padx=4, pady=4)

        # Jobs
        tk.Label(frm, text="并行作业数: ").grid(row=5, column=0, sticky="w", padx=4, pady=4)
        self.jobs_spin = tk.Spinbox(frm, from_=1, to=(os.cpu_count() or 1), width=6)
        self.jobs_spin.delete(0, tk.END)
        self.jobs_spin.insert(0, str(os.cpu_count() or 1))
        self.jobs_spin.grid(row=5, column=1, sticky="w", padx=4, pady=4)

        # Track offset
        tk.Label(frm, text="轨号偏移: ").grid(row=5, column=2, sticky="w", padx=4, pady=4)
        self.track_offset_spin = tk.Spinbox(frm, from_=-99, to=999, width=6)
        self.track_offset_spin.delete(0, tk.END)
        self.track_offset_spin.insert(0, "0")
        self.track_offset_spin.grid(row=5, column=3, sticky="w", padx=4, pady=4)

        # Checkbuttons
        self.overwrite_var = tk.BooleanVar(value=False)
        tk.Checkbutton(frm, text="覆盖已有文件", variable=self.overwrite_var).grid(
            row=6, column=1, sticky="w", padx=4, pady=4
        )
        self.nometa_var = tk.BooleanVar(value=False)
        tk.Checkbutton(frm, text="不写入元数据", variable=self.nometa_var).grid(
            row=7, column=1, sticky="w", padx=4, pady=4
        )

        # Disc number writing controls (checkbox + spinbox)
        self.write_disc_var = tk.BooleanVar(value=False)
        def _on_write_disc_changed():
            state = tk.NORMAL if self.write_disc_var.get() else tk.DISABLED
            try:
                self.disc_spin.config(state=state)
            except Exception:
                pass

        tk.Checkbutton(frm, text="写入盘号", variable=self.write_disc_var, command=_on_write_disc_changed).grid(
            row=6, column=2, sticky="w", padx=4, pady=4
        )
        self.disc_spin = tk.Spinbox(frm, from_=1, to=99, width=4, state=tk.DISABLED)
        self.disc_spin.delete(0, tk.END)
        self.disc_spin.insert(0, "1")
        self.disc_spin.grid(row=6, column=3, sticky="w", padx=4, pady=4)

        # Buttons
        btn_frame = tk.Frame(frm)
        btn_frame.grid(row=8, column=0, columnspan=5, pady=(8, 0))
        self.start_btn = tk.Button(btn_frame, text="开始分割", command=self.start_split)
        self.start_btn.pack(side=tk.LEFT, padx=4)
        tk.Button(btn_frame, text="退出", command=self.quit).pack(side=tk.LEFT, padx=4)

        # Log
        tk.Label(frm, text="状态: ").grid(row=9, column=0, sticky="nw", padx=4, pady=4)
        log_frame = tk.Frame(frm)
        log_frame.grid(row=9, column=1, columnspan=4, sticky="nsew", padx=4, pady=4)
        self.log = tk.Text(log_frame, height=10)
        scrollbar = tk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        frm.grid_rowconfigure(9, weight=1)
        frm.grid_columnconfigure(1, weight=1)

    def browse_cue(self):
        p = filedialog.askopenfilename(title="选择 CUE 文件", filetypes=[("CUE", "*.cue"), ("All", "*")])
        if p:
            self.cue_entry.delete(0, tk.END)
            self.cue_entry.insert(0, p)

    def browse_audio(self):
        p = filedialog.askopenfilename(title="选择 音频 文件", filetypes=[("Audio", "*.flac *.wav *.mp3 *.ape *.wma *.m4a"), ("All", "*")])
        if p:
            self.audio_entry.delete(0, tk.END)
            self.audio_entry.insert(0, p)

    def browse_output(self):
        p = filedialog.askdirectory(title="选择 输出 目录")
        if p:
            self.out_entry.delete(0, tk.END)
            self.out_entry.insert(0, p)

    def log_msg(self, msg: str):
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)

    def set_ui_state(self, enabled: bool):
        state = tk.NORMAL if enabled else tk.DISABLED
        for w in [self.start_btn]:
            w.config(state=state)

    def start_split(self):
        cue = self.cue_entry.get().strip()
        if not cue:
            messagebox.showwarning("提示", "请指定 CUE 文件")
            return
        if not os.path.exists(cue):
            messagebox.showerror("错误", "找不到指定的 CUE 文件")
            return

        args = {
            "cue_file": cue,
            "audio_file": self.audio_entry.get().strip() or None,
            "output_dir": self.out_entry.get().strip() or ".",
            "format": self.format_var.get(),
            "cue_encoding": self.encoding_entry.get().strip() or "utf-8",
            "overwrite": bool(self.overwrite_var.get()),
            "no_metadata": bool(self.nometa_var.get()),
            "jobs": int(self.jobs_spin.get()),
            "track_offset": int(self.track_offset_spin.get()),
            "write_disc": bool(self.write_disc_var.get()),
            "disc_number": int(self.disc_spin.get()) if bool(self.write_disc_var.get()) else None,
        }

        self.log_msg("开始：" + str(args))
        self.set_ui_state(False)

        def worker():
            try:
                res = self.split_func(**args)
                if res:
                    self.log_msg("完成：所有轨道已成功处理。")
                    messagebox.showinfo("完成", "所有轨道已成功处理。")
                else:
                    self.log_msg("完成：存在错误，部分轨道处理失败。")
                    messagebox.showwarning("完成", "存在错误，部分轨道处理失败。请查看日志。")
            except Exception:
                tb = traceback.format_exc()
                self.log_msg("异常：" + tb)
                messagebox.showerror("异常", "运行中发生异常，详情在日志中。")
            finally:
                self.set_ui_state(True)

        t = threading.Thread(target=worker, daemon=True)
        t.start()


def main():
    app = App(split_cue)
    app.mainloop()


if __name__ == "__main__":
    main()
