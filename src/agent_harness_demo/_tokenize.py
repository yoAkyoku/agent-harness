"""Isolated pinned Windows vocab-only worker. No inference context or decoding."""
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import sys


class Params(C.Structure):
    # Ollama's audited b11803 ABI; reject unverified DLLs before calling it.
    _fields_ = [("devices", C.c_void_p), ("tensor_buft_overrides", C.c_void_p),
        ("n_gpu_layers", C.c_int32), ("split_mode", C.c_int), ("load_mode", C.c_int),
        ("lazy_mode", C.c_int), ("main_gpu", C.c_int32), ("tensor_split", C.c_void_p),
        ("progress_callback", C.c_void_p), ("progress_callback_user_data", C.c_void_p),
        ("kv_overrides", C.c_void_p), ("vocab_only", C.c_bool), ("check_tensors", C.c_bool),
        ("use_extra_bufts", C.c_bool), ("no_host", C.c_bool), ("no_alloc", C.c_bool), ("load_mtp", C.c_bool)]


def main() -> None:
    library, model_path = Path(sys.argv[1]), Path(sys.argv[2])
    digest = hashlib.sha256(library.read_bytes()).hexdigest()
    if digest != "96a590dd1a1a13c25d3d40dbfba7e5114fd691be4d318a1a7c0b7ee6a02636df" or C.sizeof(Params) != 80:
        raise ValueError("Unsupported tokenizer ABI.")
    data = sys.stdin.read(6291457)
    if len(data) > 6291456:
        raise ValueError("Oversized tokenizer request.")
    text = json.loads(data)["prompt"].encode("utf-8")
    if len(text) > 1048576:
        raise ValueError("Oversized prompt.")
    directory = os.add_dll_directory(str(library.parent))
    lib = C.CDLL(str(library))
    lib.llama_model_default_params.restype = Params
    lib.llama_model_load_from_file.argtypes = [C.c_char_p, Params]
    lib.llama_model_load_from_file.restype = C.c_void_p
    lib.llama_model_get_vocab.argtypes = [C.c_void_p]
    lib.llama_model_get_vocab.restype = C.c_void_p
    lib.llama_vocab_n_tokens.argtypes = [C.c_void_p]
    lib.llama_vocab_n_tokens.restype = C.c_int32
    lib.llama_tokenize.argtypes = [C.c_void_p, C.c_char_p, C.c_int32, C.POINTER(C.c_int32), C.c_int32, C.c_bool, C.c_bool]
    lib.llama_tokenize.restype = C.c_int32
    lib.llama_model_free.argtypes = [C.c_void_p]
    params = lib.llama_model_default_params()
    params.vocab_only = True
    model = lib.llama_model_load_from_file(os.fsencode(model_path), params)
    if not model:
        raise ValueError("Vocab load failed.")
    try:
        vocab = lib.llama_model_get_vocab(model)
        if lib.llama_vocab_n_tokens(vocab) != 248320:
            raise ValueError("Unsupported vocabulary.")
        tokens = (C.c_int32 * (len(text) + 64))()
        count = lib.llama_tokenize(vocab, text, len(text), tokens, len(tokens), True, True)
        if not 0 <= count <= len(tokens):
            raise ValueError("Token count failed.")
        print(json.dumps({"input_tokens":count,"library_sha256":digest}))
    finally:
        lib.llama_model_free(model)
        directory.close()


if __name__ == "__main__":
    main()
