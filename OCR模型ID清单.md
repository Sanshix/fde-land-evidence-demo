# OCR 模型 ID 清单（测试用）

> 全部 ID 已逐个核对存在（ModelScope API / 平台文档）。API 侧模型上下架以平台 `/v1/models` 为准。

---

## 一、聚合 API：一个 Key 直接调用

### 硅基流动 SiliconFlow
`base_url: https://api.siliconflow.cn/v1`

```
PaddlePaddle/PaddleOCR-VL-1.5
PaddlePaddle/PaddleOCR-VL
deepseek-ai/DeepSeek-OCR
deepseek-ai/DeepSeek-OCR-2
deepseek-ai/deepseek-vl2
Qwen/Qwen3-VL-2B-Instruct
Qwen/Qwen3-VL-4B-Instruct
Qwen/Qwen3-VL-8B-Instruct
Qwen/Qwen3-VL-32B-Instruct
Qwen/Qwen3-VL-30B-A3B-Instruct
Qwen/Qwen2.5-VL-7B-Instruct
zai-org/GLM-4.5V
```

### 百度千帆
`base_url: https://qianfan.baidubce.com/v2`（OCR 专用接口部分非 OpenAI 协议）

```
paddleocr-vl
paddleocrv6
pp-structurev3
deepseek-ocr
qianfan-ocr
qianfan-ocr-fast
qwen3-32b
qwen2.5-vl-7b-instruct
```

### 阿里云百炼
`base_url: https://dashscope.aliyuncs.com/compatible-mode/v1`

```
qwen3.5-ocr
qwen-vl-ocr-latest
qwen-vl-ocr-2025-11-20
qwen-vl-ocr
qwen-vl-max-latest
qwen-vl-plus-latest
qwen2.5-vl-72b-instruct
qwen2.5-vl-32b-instruct
qwen2.5-vl-7b-instruct
qwen2.5-vl-3b-instruct
```

### 302.AI
`base_url: https://api.302.ai`

```
PaddleOCR-VL-0.9B
DeepSeek-OCR
GLM-OCR
MinerU
```

### AIHubMix
`base_url: https://aihubmix.com/v1`

```
paddleocr-vl-0.9b
qianfan-ocr-fast
```

---

## 二、政务云里可能已有 / 可采购（本地部署 ID）

### PaddleOCR 检测 + 识别
```
PaddlePaddle/PP-OCRv6_medium_det
PaddlePaddle/PP-OCRv6_medium_rec
PaddlePaddle/PP-OCRv6_small_det
PaddlePaddle/PP-OCRv6_small_rec
PaddlePaddle/PP-OCRv6_tiny_det
PaddlePaddle/PP-OCRv6_tiny_rec
PaddlePaddle/PP-OCRv5_server_det
PaddlePaddle/PP-OCRv5_server_rec
PaddlePaddle/PP-OCRv5_mobile_det
PaddlePaddle/PP-OCRv5_mobile_rec
PaddlePaddle/PP-OCRv4_server_det
PaddlePaddle/PP-OCRv4_server_rec
PaddlePaddle/PP-OCRv4_mobile_det
PaddlePaddle/PP-OCRv4_mobile_rec
PaddlePaddle/PP-OCRv3_mobile_rec
```

### PaddleOCR 版面分析 + 预处理
```
PaddlePaddle/PP-DocLayoutV2
PaddlePaddle/PP-DocLayoutV3
PaddlePaddle/PP-DocLayout-L
PaddlePaddle/PP-DocLayout-S
PaddlePaddle/PP-DocLayout_plus-L
PaddlePaddle/PP-LCNet_x1_0_doc_ori
PaddlePaddle/UVDoc
```

### PaddleOCR 文档解析 VLM
```
PaddlePaddle/PaddleOCR-VL
PaddlePaddle/PaddleOCR-VL-1.5
PaddlePaddle/PaddleOCR-VL-1.6
PaddlePaddle/PaddleOCR-VL-1.6-GGUF
PaddlePaddle/PP-DocBee-2B
```

### DeepSeek OCR
```
deepseek-ai/DeepSeek-OCR
deepseek-ai/DeepSeek-OCR-2
```

### 千问 VL（政务云存量最多）
```
Qwen/Qwen2.5-VL-3B-Instruct
Qwen/Qwen2.5-VL-7B-Instruct
Qwen/Qwen2.5-VL-32B-Instruct
Qwen/Qwen2.5-VL-72B-Instruct
```

### 智谱 / InternVL / 文档解析
```
ZhipuAI/GLM-4.5V
ZhipuAI/GLM-4.1V-9B-Thinking
OpenGVLab/InternVL3-2B
OpenGVLab/InternVL3-8B
OpenGVLab/InternVL3-14B
OpenGVLab/InternVL3-38B
OpenGVLab/InternVL3-78B
opendatalab/MinerU2.5-2509-1.2B
RapidAI/RapidOCR
```

---

## 三、政务云暂无、但国产 CPU 可本地部署（补测）

### 轻量（≤4B，CPU 可量产测试）
```
Qwen/Qwen3-VL-2B-Instruct
Qwen/Qwen3-VL-2B-Thinking
Qwen/Qwen3-VL-2B-Instruct-GGUF
Qwen/Qwen3-VL-4B-Instruct
Qwen/Qwen3-VL-4B-Thinking
Qwen/Qwen3-VL-4B-Instruct-GGUF
OpenGVLab/InternVL3_5-1B
OpenGVLab/InternVL3_5-2B
OpenGVLab/InternVL3-1B
stepfun-ai/GOT-OCR2_0
openbmb/MiniCPM-V-4_5
openbmb/MiniCPM-V-4_5-gguf
```

### 中大型（8B+，CPU 仅验证/较慢）
```
Qwen/Qwen3-VL-8B-Instruct
Qwen/Qwen3-VL-8B-Thinking
Qwen/Qwen3-VL-8B-Instruct-GGUF
Qwen/Qwen3-VL-32B-Instruct
Qwen/Qwen3-VL-32B-Thinking
Qwen/Qwen3-VL-32B-Instruct-GGUF
Qwen/Qwen3-VL-30B-A3B-Instruct
Qwen/Qwen3-VL-30B-A3B-Thinking
Qwen/Qwen3-VL-30B-A3B-Instruct-GGUF
Qwen/Qwen3-VL-235B-A22B-Instruct
Qwen/Qwen3-VL-235B-A22B-Instruct-GGUF
OpenGVLab/InternVL3_5-8B
OpenGVLab/InternVL3_5-14B
OpenGVLab/InternVL3_5-38B
dots-studio/dots.ocr
nanonets/Nanonets-OCR-s
allenai/olmOCR-2-7B-1025
```
