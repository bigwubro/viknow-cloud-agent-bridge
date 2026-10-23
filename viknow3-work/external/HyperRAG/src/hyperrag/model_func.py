from dataclasses import dataclass
import numpy as np

VLLM_PRIORITY_AGENT = 0
VLLM_PRIORITY_INDEX = 10


def extra_body_with_vllm_priority(
    extra_body: dict | None = None,
    *,
    priority: int = VLLM_PRIORITY_INDEX,
) -> dict:
    body = dict(extra_body or {})
    body.setdefault("priority", int(priority))
    return body


@dataclass
class ModelConfig:
    """模型配置 - 可序列化"""

    base_url: str
    api_key: str = "EMPTY"
    model: str = "default"
    provider: str = "vllm"  # vllm 或 openai
    max_retries: int = 3
    timeout: float = 60.0

    def create_client(self):
        """根据provider创建AsyncOpenAI客户端"""
        from openai import AsyncOpenAI

        if self.provider == "openai":
            # OpenAI官方API - 不传base_url，使用默认
            return AsyncOpenAI(
                api_key=self.api_key, max_retries=self.max_retries, timeout=self.timeout
            )
        elif self.provider == "vllm":
            # vLLM部署 - 需要自定义base_url
            return AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                max_retries=self.max_retries,
                timeout=self.timeout,
            )
        else:
            raise ValueError(f"不支持的provider: {self.provider}")


class LLMFunc:
    """LLM函数封装 - 用于HyperRAG的llm_model_func

    不保存 AsyncOpenAI 实例（包含不可序列化的 RLock），
    而是保存配置，在调用时临时创建客户端。
    """

    def __init__(
        self,
        config: ModelConfig,
        max_tokens: int = 1024,
        presence_penalty: float = 1.2,
        temperature: float = 0.7,
        top_p: float = 0.95,
    ):
        self.config = config
        self.max_tokens = max_tokens
        self.presence_penalty = presence_penalty
        self.temperature = temperature
        self.top_p = top_p

    async def __call__(self, prompt, system_prompt=None, history_messages=None, **kwargs):
        # 根据provider创建客户端
        client = self.config.create_client()

        messages = history_messages.copy() if history_messages else []

        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = await client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            max_tokens=kwargs.get("max_tokens", self.max_tokens),
            presence_penalty=self.presence_penalty,
            temperature=self.temperature,
            top_p=self.top_p,
            extra_body=extra_body_with_vllm_priority(),
        )
        try:
            from hyperrag.observability.index_usage import (
                record_generation,
                usage_from_openai_response,
            )

            record_generation(
                name="llm",
                model=self.config.model,
                usage=usage_from_openai_response(response),
            )
        except Exception:
            pass
        await client.close()
        return response.choices[0].message.content


class EmbedFunc:
    """Embedding函数封装 - 用于HyperRAG的embed_model_func

    不保存 AsyncOpenAI 实例，而是保存配置，在调用时临时创建客户端。
    """

    def __init__(self, config: ModelConfig):
        self.config = config

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        """
        生成文本embedding

        Args:
            texts: 文本列表

        Returns:
            embedding向量列表
        """
        # 根据provider创建客户端
        client = self.config.create_client()

        response = await client.embeddings.create(model=self.config.model, input=texts)
        try:
            from hyperrag.observability.index_usage import (
                record_generation,
                usage_from_openai_response,
            )

            record_generation(
                name="embedding",
                model=self.config.model,
                usage=usage_from_openai_response(response),
            )
        except Exception:
            pass
        await client.close()
        return np.array([item.embedding for item in response.data])


class ImageCaptionFunc:
    """图片Caption函数封装 - 用于HyperRAG的vision_model_func

    不保存 AsyncOpenAI 实例，而是保存配置，在调用时临时创建客户端。
    """

    def __init__(self, config: ModelConfig, max_tokens: int = 1024):
        self.config = config
        self.max_tokens = max_tokens

    async def __call__(self, image_path: str, prompt: str, **kwargs):
        """
        生成图片描述

        Args:
            image_path: 图片路径
            prompt: 提示词

        Returns:
            图片描述文本
        """
        import base64

        # 根据provider创建客户端
        client = self.config.create_client()

        with open(image_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode("utf-8")

        response = await client.chat.completions.create(
            model=self.config.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{encoded_string}"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            max_tokens=self.max_tokens,
            extra_body=extra_body_with_vllm_priority(),
        )
        try:
            from hyperrag.observability.index_usage import (
                record_generation,
                usage_from_openai_response,
            )

            record_generation(
                name="image_caption",
                model=self.config.model,
                usage=usage_from_openai_response(response),
            )
        except Exception:
            pass
        await client.close()
        return response.choices[0].message.content


class VideoCaptionFunc:
    """视频Caption函数封装 - 用于HyperRAG的video_caption_func"""

    def __init__(self, config: ModelConfig, max_tokens: int = 1024, bucket: str = "viknow"):
        self.config = config
        self.max_tokens = max_tokens
        self.bucket = bucket

    async def __call__(self, video_path: str, begin_ts, end_ts, prompt=None, uuid=None, **kwargs):
        import base64
        from pathlib import Path

        client = self.config.create_client()

        is_video = Path(video_path).suffix.lower() in [
            ".mp4",
            ".avi",
            ".mov",
            ".mkv",
            ".webm",
            ".flv",
        ]

        with open(video_path, "rb") as file:
            encoded_string = base64.b64encode(file.read()).decode("utf-8")

        content = []

        if is_video:
            if uuid is None:
                content.append(
                    {
                        "type": "video_url",
                        "video_url": {"url": f"data:video/mp4;base64,{encoded_string}"},
                    }
                )
            else:
                content.append(
                    {
                        "type": "video_url",
                        "video_url": {"url": f"data:video/mp4;base64,{encoded_string}"},
                        "uuid": uuid,
                    }
                )
            content.append({"type": "text", "text": prompt or "请描述这段视频的内容"})
        else:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded_string}"},
                }
            )
            content.append({"type": "text", "text": prompt or "请描述这张图片的内容"})

        response = await client.chat.completions.create(
            model=self.config.model,
            messages=[{"role": "user", "content": content}],
            max_tokens=self.max_tokens,
            extra_body=extra_body_with_vllm_priority(),
        )
        try:
            from hyperrag.observability.index_usage import (
                record_generation,
                usage_from_openai_response,
            )

            record_generation(
                name="video_caption",
                model=self.config.model,
                usage=usage_from_openai_response(response),
            )
        except Exception:
            pass
        await client.close()
        return response.choices[0].message.content
