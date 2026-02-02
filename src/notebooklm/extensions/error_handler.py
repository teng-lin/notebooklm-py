#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
错误处理和重试机制
提供健壮的错误处理和自动重试功能
"""

import asyncio
import logging
from functools import wraps
from typing import Callable, Any, Optional

from .config import MAX_RETRIES, RETRY_DELAY

logger = logging.getLogger(__name__)


class RetryableError(Exception):
    """可重试的错误"""
    pass


class NonRetryableError(Exception):
    """不可重试的错误"""
    pass


def is_retryable_error(error: Exception) -> bool:
    """
    判断错误是否可以重试
    
    Args:
        error: 异常对象
    
    Returns:
        是否可重试
    """
    # 网络相关错误可以重试
    retryable_types = (
        ConnectionError,
        TimeoutError,
        RetryableError,
    )
    
    if isinstance(error, retryable_types):
        return True
    
    # 尝试检查 httpx 错误（如果安装了）
    try:
        import httpx
        if isinstance(error, (
            httpx.NetworkError,
            httpx.TimeoutException,
            httpx.ConnectError,
        )):
            return True
        
        # HTTP 5xx 错误可以重试
        if isinstance(error, httpx.HTTPStatusError):
            if 500 <= error.response.status_code < 600:
                return True
            # 速率限制错误可以重试
            if error.response.status_code == 429:
                return True
    except ImportError:
        pass
    
    # 其他错误不重试
    return False


def retry_on_error(max_retries: int = MAX_RETRIES, delay: float = RETRY_DELAY):
    """
    装饰器：自动重试失败的异步函数
    
    Args:
        max_retries: 最大重试次数
        delay: 重试延迟（秒）
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                    
                except Exception as e:
                    last_exception = e
                    
                    # 检查是否可以重试
                    if not is_retryable_error(e):
                        logger.error(f"不可重试的错误: {e}")
                        raise
                    
                    # 如果还有重试机会
                    if attempt < max_retries:
                        wait_time = delay * (2 ** attempt)  # 指数退避
                        logger.warning(
                            f"操作失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}"
                        )
                        logger.info(f"等待 {wait_time:.1f} 秒后重试...")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"达到最大重试次数 ({max_retries})")
            
            # 所有重试都失败
            raise last_exception
        
        return wrapper
    return decorator


class ErrorHandler:
    """错误处理器"""
    
    def __init__(self, max_retries: int = MAX_RETRIES, retry_delay: int = RETRY_DELAY):
        self.max_retries = max_retries
        self.retry_delay = retry_delay
    
    @staticmethod
    def handle_error(error: Exception, context: str = "") -> None:
        """Log error with context"""
        error_msg = f"Error in {context}: {str(error)}" if context else str(error)
        logger.error(error_msg, exc_info=True)
    
    @staticmethod
    def safe_execute(func: Callable, *args, default: Any = None, **kwargs) -> Any:
        """Execute function safely, return default on error"""
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error executing {func.__name__}: {str(e)}")
            return default


class ErrorLogger:
    """错误日志记录器"""
    
    def __init__(self, log_file: Optional[str] = None):
        """
        初始化错误日志记录器
        
        Args:
            log_file: 日志文件路径（可选）
        """
        self.log_file = log_file
        self.errors = []
    
    def log_error(self, context: str, error: Exception, video_info: Optional[dict] = None):
        """
        记录错误
        
        Args:
            context: 错误上下文
            error: 异常对象
            video_info: 视频信息（可选）
        """
        error_record = {
            'context': context,
            'error_type': type(error).__name__,
            'error_message': str(error),
            'video_info': video_info
        }
        
        self.errors.append(error_record)
        
        # 记录到日志
        log_message = f"错误 [{context}]: {error}"
        if video_info:
            log_message += f" | 视频: {video_info.get('youtube_title', 'Unknown')}"
        
        logger.error(log_message)
        
        # 如果指定了日志文件，追加写入
        if self.log_file:
            try:
                with open(self.log_file, 'a', encoding='utf-8') as f:
                    f.write(f"{log_message}\n")
            except Exception as e:
                logger.error(f"写入错误日志文件失败: {e}")
    
    def get_error_summary(self) -> dict:
        """
        获取错误摘要
        
        Returns:
            错误统计字典
        """
        if not self.errors:
            return {'total_errors': 0}
        
        summary = {
            'total_errors': len(self.errors),
            'error_types': {}
        }
        
        for error in self.errors:
            error_type = error['error_type']
            summary['error_types'][error_type] = \
                summary['error_types'].get(error_type, 0) + 1
        
        return summary
    
    def print_summary(self):
        """打印错误摘要"""
        summary = self.get_error_summary()
        
        if summary['total_errors'] == 0:
            logger.info("✅ 没有错误发生")
            return
        
        logger.info(f"\n错误摘要:")
        logger.info(f"  总错误数: {summary['total_errors']}")
        logger.info(f"  错误类型分布:")
        for error_type, count in summary['error_types'].items():
            logger.info(f"    - {error_type}: {count}")


def safe_execute(func: Callable, *args, default_return=None, **kwargs) -> Any:
    """
    安全执行函数，捕获所有异常
    
    Args:
        func: 要执行的函数
        *args: 位置参数
        default_return: 发生错误时的默认返回值
        **kwargs: 关键字参数
    
    Returns:
        函数返回值或默认值
    """
    try:
        return func(*args, **kwargs)
    except Exception as e:
        logger.error(f"执行函数 {func.__name__} 时出错: {e}")
        return default_return


async def safe_execute_async(func: Callable, *args, default_return=None, **kwargs) -> Any:
    """
    安全执行异步函数，捕获所有异常
    
    Args:
        func: 要执行的异步函数
        *args: 位置参数
        default_return: 发生错误时的默认返回值
        **kwargs: 关键字参数
    
    Returns:
        函数返回值或默认值
    """
    try:
        return await func(*args, **kwargs)
    except Exception as e:
        logger.error(f"执行异步函数 {func.__name__} 时出错: {e}")
        return default_return
