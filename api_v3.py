"""
# WebAPI文档 (3.0) - 使用了缓存技术，初始化时使用LRU Cache TTS 实例，缓存加载模型的世界，达到减少切换不同语音时的推理时间

` python api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml `

## 执行参数:
    `-a` - `绑定地址, 默认"127.0.0.1"`
    `-p` - `绑定端口, 默认9880`
    `-c` - `TTS配置文件路径, 默认"GPT_SoVITS/configs/tts_infer.yaml"`

## 调用:

### 推理

endpoint: `/tts`
GET:
```
http://127.0.0.1:9880/tts?text=先帝创业未半而中道崩殂，今天下三分，益州疲弊，此诚危急存亡之秋也。&text_lang=zh&ref_audio_path=archive_jingyuan_1.wav&prompt_lang=zh&prompt_text=我是「罗浮」云骑将军景元。不必拘谨，「将军」只是一时的身份，你称呼我景元便可&text_split_method=cut5&batch_size=1&media_type=wav&streaming_mode=true
```

POST:
```json
{
    "text": "",                                                 # str.(required) text to be synthesized
    "text_lang": "",                                            # str.(required) language of the text to be synthesized
    "ref_audio_path": "",                                       # str.(required) reference audio path.
    "prompt_text": "",                                          # str.(optional) prompt text for the reference audio
    "prompt_lang": "",                                          # str.(required) language of the prompt text for the reference audio
    "top_k": 5,                                                 # int.(optional) top k sampling
    "top_p": 1,                                                 # float.(optional) top p sampling
    "temperature": 1,                                           # float.(optional) temperature for sampling
    "text_split_method": "cut5",                                # str.(optional) text split method, see text_segmentation_method.py for details.
    "batch_size": 1,                                            # int.(optional) batch size for inference
    "batch_threshold": 0.75,                                    # float.(optional) threshold for batch splitting.
    "split_bucket": true,                                       # bool.(optional) whether to split the batch into multiple buckets.
    "speed_factor":1.0,                                         # float.(optional) control the speed of the synthesized audio.
    "fragment_interval":0.3,                                    # float.(optional) to control the interval of the audio fragment.
    "seed": -1,                                                 # int.(optional) random seed for reproducibility.
    "media_type": "wav",                                        # str.(optional) media type of the output audio, support "wav", "raw", "ogg", "aac".
    "streaming_mode": false,                                    # bool.(optional) whether to return a streaming response.
    "parallel_infer": True,                                     # bool.(optional) whether to use parallel inference.
    "repetition_penalty": 1.35,                                 # float.(optional) repetition penalty for T2S model.
    "tts_infer_yaml_path": “GPT_SoVITS/configs/tts_infer.yaml”  # str.(optional) tts infer yaml path
}
```

RESP:
成功: 直接返回 wav 音频流， http code 200
失败: 返回包含错误信息的 json, http code 400

### 命令控制

endpoint: `/control`

command:
"restart": 重新运行
"exit": 结束运行

GET:
```
http://127.0.0.1:9880/control?command=restart
```
POST:
```json
{
    "command": "restart"
}
```

RESP: 无


### 切换GPT模型

endpoint: `/set_gpt_weights`

GET:
```
http://127.0.0.1:9880/set_gpt_weights?weights_path=GPT_SoVITS/pretrained_models/s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt
```
RESP:
成功: 返回"success", http code 200
失败: 返回包含错误信息的 json, http code 400


### 切换Sovits模型

endpoint: `/set_sovits_weights`

GET:
```
http://127.0.0.1:9880/set_sovits_weights?weights_path=GPT_SoVITS/pretrained_models/s2G488k.pth
```

RESP:
成功: 返回"success", http code 200
失败: 返回包含错误信息的 json, http code 400

"""

import os
import sys
import traceback
from typing import Generator

import torch

now_dir = os.getcwd()
sys.path.append(now_dir)
sys.path.append("%s/GPT_SoVITS" % (now_dir))

import argparse
import glob
import json
import signal
import subprocess
import wave
from functools import lru_cache
from io import BytesIO
from typing import Optional, Union

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from GPT_SoVITS.TTS_infer_pack.text_segmentation_method import (
    get_method_names as get_cut_method_names,
)
from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config

cut_method_names = get_cut_method_names()

parser = argparse.ArgumentParser(description="GPT-SoVITS api")
parser.add_argument("-a", "--bind_addr", type=str, default="0.0.0.0", help="default: 0.0.0.0")
parser.add_argument("-p", "--port", type=int, default="9880", help="default: 9880")
args = parser.parse_args()
port = args.port
host = args.bind_addr
argv = sys.argv

APP = FastAPI(servers=[{"url": "https://cloud-gateway.ces.myfiinet.com/ai-audio/tts"}, {"url": "http://10.20.216.222:6616"}])

SPEAKER_HOME_DIR = "/workspace/reference"  # 會放audio, 還有一個對應speaker的json

speakers: dict[str, "Speaker"] = {}


class Speaker(BaseModel):
    name: str
    prompt_text: Union[str , None] = None
    prompt_lang: Union[str , None] = None

    @property
    def audio_path(self):
        return os.path.join(SPEAKER_HOME_DIR, f"{self.name}.wav")

    @classmethod
    def get_by_name(cls, name: str):
        if name in speakers:
            return speakers[name]
        speaker_json_path = os.path.join(SPEAKER_HOME_DIR, f"{name}.json")
        if not os.path.exists(speaker_json_path):
            print(f"speaker {name} not found")
            return None
        with open(speaker_json_path, "r") as f:
            speaker_data = json.load(f)
        found_speaker = Speaker.model_validate(speaker_data)

        if not os.path.exists(found_speaker.audio_path):
            print(f"speaker {name} found without audio file")
            return None

        speakers[name] = found_speaker
        return speakers[name]

    def update(self, audio: Union[UploadFile ,None], prompt_lang: Union[str ,None], prompt_text: Union[str , None]):
        # 確認audio.filename是wav
        if audio is not None:
            if not audio.filename.endswith(".wav"):
                raise HTTPException(status_code=400, detail="audio must be a wav file")
            # 使用audio覆蓋{name}.wav
            audio_path = os.path.join(SPEAKER_HOME_DIR, f"{self.name}.wav")
            with open(audio_path, "wb") as f:
                f.write(audio.file.read())
        if prompt_lang is not None:
            self.prompt_lang = prompt_lang
        if prompt_text is not None:
            self.prompt_text = prompt_text
        # 更新json
        speaker_json_path = os.path.join(SPEAKER_HOME_DIR, f"{self.name}.json")
        with open(speaker_json_path, "w") as f:
            json.dump(self.model_dump(), f)

        # ensure self is the one in speakers
        speakers[self.name] = self


class CustomOpenAPIMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        base_url = str(request.base_url)

        print(f"call from {base_url}")
        if "cloud-gateway.ces.myfiinet.com" in base_url:
            APP.openapi_url = "/ai-audio/tts/openapi.json"
        else:
            APP.openapi_url = "openapi.json"
        return response


APP.add_middleware(CustomOpenAPIMiddleware)


class TTS_Request(BaseModel):
    text: str = None
    text_lang: str = None
    ref_audio_path: str = None
    prompt_lang: str = None
    prompt_text: str = ""
    top_k: int = 5
    top_p: float = 1
    temperature: float = 1
    text_split_method: str = "cut5"
    batch_size: int = 1
    batch_threshold: float = 0.75
    split_bucket: bool = True
    speed_factor: float = 1.0
    fragment_interval: float = 0.3
    seed: int = -1
    media_type: str = "wav"
    streaming_mode: bool = False
    parallel_infer: bool = True
    repetition_penalty: float = 1.35
    tts_infer_yaml_path: str = None
    """推理时需要加载的声音模型的yaml配置文件路径，如：GPT_SoVITS/configs/tts_infer.yaml"""


@lru_cache(maxsize=10)
def get_tts_instance(tts_config: TTS_Config) -> TTS:
    # 此方法使用config path當作key, 維持tts instance, 故假設我限制只使用同一位置之config path
    # 則speaker為singleton
    print(f"load tts config from {tts_config.configs_path}")
    return TTS(tts_config)


def pack_ogg(io_buffer: BytesIO, data: np.ndarray, rate: int):
    """modify from https://github.com/RVC-Boss/GPT-SoVITS/pull/894/files"""
    with sf.SoundFile(io_buffer, mode="w", samplerate=rate, channels=1, format="ogg") as audio_file:
        audio_file.write(data)
    return io_buffer


def pack_raw(io_buffer: BytesIO, data: np.ndarray, rate: int):
    io_buffer.write(data.tobytes())
    return io_buffer


def pack_wav(io_buffer: BytesIO, data: np.ndarray, rate: int):
    io_buffer = BytesIO()
    sf.write(io_buffer, data, rate, format="wav")
    return io_buffer


def pack_aac(io_buffer: BytesIO, data: np.ndarray, rate: int):
    process = subprocess.Popen(
        [
            "ffmpeg",
            "-f",
            "s16le",  # 输入16位有符号小端整数PCM
            "-ar",
            str(rate),  # 设置采样率
            "-ac",
            "1",  # 单声道
            "-i",
            "pipe:0",  # 从管道读取输入
            "-c:a",
            "aac",  # 音频编码器为AAC
            "-b:a",
            "192k",  # 比特率
            "-vn",  # 不包含视频
            "-f",
            "adts",  # 输出AAC数据流格式
            "pipe:1",  # 将输出写入管道
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out, _ = process.communicate(input=data.tobytes())
    io_buffer.write(out)
    return io_buffer


def pack_audio(io_buffer: BytesIO, data: np.ndarray, rate: int, media_type: str):
    if media_type == "ogg":
        io_buffer = pack_ogg(io_buffer, data, rate)
    elif media_type == "aac":
        io_buffer = pack_aac(io_buffer, data, rate)
    elif media_type == "wav":
        io_buffer = pack_wav(io_buffer, data, rate)
    else:
        io_buffer = pack_raw(io_buffer, data, rate)
    io_buffer.seek(0)
    return io_buffer


# from https://huggingface.co/spaces/coqui/voice-chat-with-mistral/blob/main/app.py
def wave_header_chunk(frame_input=b"", channels=1, sample_width=2, sample_rate=32000):
    # This will create a wave header then append the frame input
    # It should be first on a streaming wav file
    # Other frames better should not have it (else you will hear some artifacts each chunk start)
    wav_buf = BytesIO()
    with wave.open(wav_buf, "wb") as vfout:
        vfout.setnchannels(channels)
        vfout.setsampwidth(sample_width)
        vfout.setframerate(sample_rate)
        vfout.writeframes(frame_input)

    wav_buf.seek(0)
    return wav_buf.read()


def handle_control(command: str):
    if command == "restart":
        os.execl(sys.executable, sys.executable, *argv)
    elif command == "exit":
        os.kill(os.getpid(), signal.SIGTERM)
        exit(0)


def check_params(req: dict, tts_config: TTS_Config):
    text: str = req.get("text", "")
    text_lang: str = req.get("text_lang", "")
    ref_audio_path: str = req.get("ref_audio_path", "")
    streaming_mode: bool = req.get("streaming_mode", False)
    media_type: str = req.get("media_type", "wav")
    prompt_lang: str = req.get("prompt_lang", "")
    text_split_method: str = req.get("text_split_method", "cut5")

    if ref_audio_path in [None, ""]:
        return JSONResponse(status_code=400, content={"message": "ref_audio_path is required"})
    if text in [None, ""]:
        return JSONResponse(status_code=400, content={"message": "text is required"})
    if text_lang in [None, ""]:
        return JSONResponse(status_code=400, content={"message": "text_lang is required"})
    elif text_lang.lower() not in tts_config.languages:
        return JSONResponse(status_code=400, content={"message": "text_lang is not supported"})
    if prompt_lang in [None, ""]:
        return JSONResponse(status_code=400, content={"message": "prompt_lang is required"})
    elif prompt_lang.lower() not in tts_config.languages:
        return JSONResponse(status_code=400, content={"message": "prompt_lang is not supported"})
    if media_type not in ["wav", "raw", "ogg", "aac"]:
        return JSONResponse(status_code=400, content={"message": "media_type is not supported"})
    elif media_type == "ogg" and not streaming_mode:
        return JSONResponse(status_code=400, content={"message": "ogg format is not supported in non-streaming mode"})

    if text_split_method not in cut_method_names:
        return JSONResponse(status_code=400, content={"message": f"text_split_method:{text_split_method} is not supported"})

    return None


async def tts_handle(req: dict):
    """
    Text to speech handler.

    Args:
        req (dict):
            {
                "text": "",                   # str.(required) text to be synthesized
                "text_lang: "",               # str.(required) language of the text to be synthesized
                "ref_audio_path": "",         # str.(required) reference audio path
                "prompt_text": "",            # str.(optional) prompt text for the reference audio
                "prompt_lang": "",            # str.(required) language of the prompt text for the reference audio
                "top_k": 5,                   # int. top k sampling
                "top_p": 1,                   # float. top p sampling
                "temperature": 1,             # float. temperature for sampling
                "text_split_method": "cut5",  # str. text split method, see text_segmentation_method.py for details.
                "batch_size": 1,              # int. batch size for inference
                "batch_threshold": 0.75,      # float. threshold for batch splitting.
                "split_bucket: True,          # bool. whether to split the batch into multiple buckets.
                "speed_factor":1.0,           # float. control the speed of the synthesized audio.
                "fragment_interval":0.3,      # float. to control the interval of the audio fragment.
                "seed": -1,                   # int. random seed for reproducibility.
                "media_type": "wav",          # str. media type of the output audio, support "wav", "raw", "ogg", "aac".
                "streaming_mode": False,      # bool. whether to return a streaming response.
                "parallel_infer": True,       # bool.(optional) whether to use parallel inference.
                "repetition_penalty": 1.35    # float.(optional) repetition penalty for T2S model.
            }
    returns:
        StreamingResponse: audio stream response.
    """

    streaming_mode = req.get("streaming_mode", False)
    media_type = req.get("media_type", "wav")

    tts_infer_yaml_path = req.get("tts_infer_yaml_path", "GPT_SoVITS/configs/tts_infer.yaml")

    tts_config = TTS_Config(tts_infer_yaml_path)
    check_res = check_params(req, tts_config)
    if check_res is not None:
        return check_res

    if streaming_mode:
        req["return_fragment"] = True

    try:
        tts_instance = get_tts_instance(tts_config)

        move_to_original(tts_instance, tts_config)

        tts_generator = tts_instance.run(req)

        if streaming_mode:

            def streaming_generator(tts_generator: Generator, media_type: str):
                if media_type == "wav":
                    yield wave_header_chunk()
                    media_type = "raw"
                for sr, chunk in tts_generator:
                    yield pack_audio(BytesIO(), chunk, sr, media_type).getvalue()
                # move_to_cpu(tts_instance)

            # _media_type = f"audio/{media_type}" if not (streaming_mode and media_type in ["wav", "raw"]) else f"audio/x-{media_type}"
            return StreamingResponse(
                streaming_generator(
                    tts_generator,
                    media_type,
                ),
                media_type=f"audio/{media_type}",
            )

        else:
            sr, audio_data = next(tts_generator)
            audio_data = pack_audio(BytesIO(), audio_data, sr, media_type).getvalue()
            move_to_cpu(tts_instance)
            return Response(audio_data, media_type=f"audio/{media_type}")
    except Exception as e:
        return JSONResponse(status_code=400, content={"message": "tts failed", "Exception": str(e)})


def move_to_cpu(tts):
    cpu_device = torch.device("cpu")
    tts.set_device(cpu_device, False)
    tts.enable_half_precision(False, False)
    print("Moved TTS models to CPU to save GPU memory.")


def move_to_original(tts: TTS, tts_config: TTS_Config):
    tts.set_device(tts_config.device, False)
    tts.enable_half_precision(tts_config.is_half, False)
    print("Moved TTS models back to original device for performance.")


@APP.get("/control")
async def control(command: str = None):
    if command is None:
        return JSONResponse(status_code=400, content={"message": "command is required"})
    handle_control(command)


@APP.get("/tts")
async def tts_get_endpoint(
    text: str = None,
    text_lang: str = None,
    speaker: str = None,  # if given, ignore ref_audio_path,  prompt_lang, and, prompt_text
    ref_audio_path: str = None,  # part of speaker (1/3)
    prompt_lang: str = None,  # part of speaker (2/3)
    prompt_text: str = "",  # part of speaker (3/3)
    top_k: int = 5,
    top_p: float = 1,
    temperature: float = 1,
    text_split_method: str = "cut0",
    batch_size: int = 1,
    batch_threshold: float = 0.75,
    split_bucket: bool = True,
    speed_factor: float = 1.0,
    fragment_interval: float = 0.3,
    seed: int = -1,
    media_type: str = "wav",
    streaming_mode: bool = False,
    parallel_infer: bool = True,
    repetition_penalty: float = 1.35,
    tts_infer_yaml_path: str = "GPT_SoVITS/configs/tts_infer.yaml",
):
    # 如果speaker有給, 則忽略ref_audio_path, prompt_lang, prompt_text
    if speaker is not None:
        speaker_obj = Speaker.get_by_name(speaker)
        if speaker_obj is None:
            return JSONResponse(status_code=400, content={"message": f"speaker {speaker} not found"})
        ref_audio_path = speaker_obj.audio_path
        prompt_lang = speaker_obj.prompt_lang
        prompt_text = speaker_obj.prompt_text

    # ensure prompt_lang is not None for stupid bug
    if prompt_lang is None:
        prompt_lang = ""

    req = {
        "text": text,
        "text_lang": text_lang.lower(),
        "ref_audio_path": ref_audio_path,
        "prompt_text": prompt_text,
        "prompt_lang": prompt_lang.lower(),
        "top_k": top_k,
        "top_p": top_p,
        "temperature": temperature,
        "text_split_method": text_split_method,
        "batch_size": int(batch_size),
        "batch_threshold": float(batch_threshold),
        "speed_factor": float(speed_factor),
        "split_bucket": split_bucket,
        "fragment_interval": fragment_interval,
        "seed": seed,
        "media_type": media_type,
        "streaming_mode": streaming_mode,
        "parallel_infer": parallel_infer,
        "repetition_penalty": float(repetition_penalty),
        "tts_infer_yaml_path": tts_infer_yaml_path,
    }

    return await tts_handle(req)


@APP.post("/tts")
async def tts_post_endpoint(request: TTS_Request):
    req = request.dict()
    return await tts_handle(req)


@APP.post("/speakers")
async def upload_speaker(
    name: str = Form(None),
    prompt_lang: Optional[str] = Form(None),
    prompt_text: Optional[str] = Form(None),
    file: UploadFile = File(...),
    # file_url: str = Form(default=None),
):
    speaker = Speaker.get_by_name(name)
    if speaker is None:
        speaker = Speaker(name=name)
    speaker.update(file, prompt_lang, prompt_text)
    return JSONResponse(status_code=200, content={"message": f"speaker {name} updated"})


@APP.get("/speakers")
async def get_speakers():
    # 檢查SPEAKER_HOME_DIR, 找所有json, 然後嘗試Speaker.get_by_name for all json
    speaker_dict_dump = {}
    for speaker_json_path in glob.glob(os.path.join(SPEAKER_HOME_DIR, "*.json")):
        speaker_name = os.path.basename(speaker_json_path).split(".")[0]
        speaker_obj = Speaker.get_by_name(speaker_name)  # will persist if exist

    speaker_dict_dump = {k: v.model_dump() for k, v in speakers.items()}
    return JSONResponse(status_code=200, content={"speakers": speaker_dict_dump})


@APP.get("/set_refer_audio")
async def set_refer_audio(refer_audio_path: str = None, tts_infer_yaml_path: str = "GPT_SoVITS/configs/tts_infer.yaml"):
    try:
        tts_config = TTS_Config(tts_infer_yaml_path)
        tts_instance = get_tts_instance(tts_config)
        tts_instance.set_ref_audio(refer_audio_path)
    except Exception as e:
        return JSONResponse(status_code=400, content={"message": "set refer audio failed", "Exception": str(e)})
    return JSONResponse(status_code=200, content={"message": "success"})


@APP.get("/set_gpt_weights")
async def set_gpt_weights(weights_path: str = None, tts_infer_yaml_path: str = "GPT_SoVITS/configs/tts_infer.yaml"):
    try:
        if weights_path in ["", None]:
            return JSONResponse(status_code=400, content={"message": "gpt weight path is required"})

        tts_config = TTS_Config(tts_infer_yaml_path)
        tts_instance = get_tts_instance(tts_config)
        tts_instance.init_t2s_weights(weights_path)
    except Exception as e:
        return JSONResponse(status_code=400, content={"message": "change gpt weight failed", "Exception": str(e)})

    return JSONResponse(status_code=200, content={"message": "success"})


@APP.get("/set_sovits_weights")
async def set_sovits_weights(weights_path: str = None, tts_infer_yaml_path: str = "GPT_SoVITS/configs/tts_infer.yaml"):
    try:
        if weights_path in ["", None]:
            return JSONResponse(status_code=400, content={"message": "sovits weight path is required"})

        tts_config = TTS_Config(tts_infer_yaml_path)
        tts_instance = get_tts_instance(tts_config)
        tts_instance.init_vits_weights(weights_path)
    except Exception as e:
        return JSONResponse(status_code=400, content={"message": "change sovits weight failed", "Exception": str(e)})
    return JSONResponse(status_code=200, content={"message": "success"})


if __name__ == "__main__":
    try:
        uvicorn.run(APP, host=host, port=port, workers=1)
    except Exception:
        traceback.print_exc()
        os.kill(os.getpid(), signal.SIGTERM)
        exit(0)
