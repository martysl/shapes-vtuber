"""Description: This file contains the implementation of the `AsyncLLM` class.
This class is responsible for handling asynchronous interaction with OpenAI API compatible
endpoints for language generation.
"""

from typing import AsyncIterator, List, Dict, Any
from openai import (
    AsyncStream,
    AsyncOpenAI,
    APIError,
    APIConnectionError,
    RateLimitError,
)
from openai.types.chat import ChatCompletionChunk
from loguru import logger

from .stateless_llm_interface import StatelessLLMInterface


class AsyncLLM(StatelessLLMInterface):
    def __init__(
        self,
        model: str,
        base_url: str,
        llm_api_key: str = "z",
        organization_id: str = "z",
        project_id: str = "z",
        temperature: float = 1.0,
    ):
        """
        Initializes an instance of the `AsyncLLM` class.

        Parameters:
        - model (str): The model to be used for language generation.
        - base_url (str): The base URL for the OpenAI API.
        - organization_id (str, optional): The organization ID for the OpenAI API. Defaults to "z".
        - project_id (str, optional): The project ID for the OpenAI API. Defaults to "z".
        - llm_api_key (str, optional): The API key for the OpenAI API. Defaults to "z".
        - temperature (float, optional): Sampling temperature, 0–2. Defaults to 1.0.
        """
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.client = AsyncOpenAI(
            base_url=base_url,
            organization=organization_id,
            project=project_id,
            api_key=llm_api_key,
        )

        logger.info(f"Initialized AsyncLLM with: {self.base_url}, model: {self.model}")

    async def chat_completion(
        self, messages: List[Dict[str, Any]], system: str = None
    ) -> AsyncIterator[str]:
        """
        Generates a chat completion using the OpenAI-compatible API asynchronously.

        Parameters:
        - messages (List[Dict[str, Any]]): The list of messages to send to the API.
        - system (str, optional): Optional system prompt.

        Yields:
        - str: Content of each streamed chunk.

        Raises:
        - APIConnectionError: When the server cannot be reached.
        - RateLimitError: When 429 status is received.
        - APIError: For other API-related errors.
        """
        logger.debug(f"Messages: {messages}")
        stream = None
        try:
            # If system prompt is provided, prepend it
            messages_with_system = messages
            if system:
                messages_with_system = [
                    {"role": "system", "content": system},
                    *messages,
                ]

            stream: AsyncStream[
                ChatCompletionChunk
            ] = await self.client.chat.completions.create(
                messages=messages_with_system,
                model=self.model,
                stream=True,
                temperature=self.temperature,
            )

            async for chunk in stream:
                if not chunk.choices:
                    logger.warning("⚠️ Received chunk with no choices: %s", chunk)
                    continue

                delta = getattr(chunk.choices[0], "delta", None)
                if not delta:
                    logger.warning("⚠️ Delta missing in chunk. Skipping. Chunk: %s", chunk)
                    continue

                content = getattr(delta, "content", "")
                if content is None:
                    content = ""

                yield content

        except APIConnectionError as e:
            logger.error(
                f"🌐 Connection error calling chat endpoint: {e.__cause__}"
            )
            yield "Error: Failed to connect to the LLM API."

        except RateLimitError as e:
            logger.error(
                f"🚫 Rate limit exceeded: {e.response}"
            )
            yield "Error: Rate limit exceeded. Please try again later."

        except APIError as e:
            logger.error(f"🔥 API error occurred: {e}")
            logger.info(f"Base URL: {self.base_url}")
            logger.info(f"Model: {self.model}")
            logger.info(f"Messages: {messages}")
            logger.info(f"Temperature: {self.temperature}")
            yield "Error: Something went wrong while generating response."

        finally:
            if stream:
                logger.debug("Chat completion finished, closing stream.")
                await stream.close()
                logger.debug("Stream closed.")
