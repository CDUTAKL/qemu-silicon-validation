"""QEMU 虚拟板卡硅后验证框架 —— 测试控制层 (harness).

子模块:
    qemu    -- QEMU 进程管理 (QemuBoard)
    qmp     -- 自实现 QMP 客户端 (QmpClient, asyncio)
    uart    -- 串口帧解析层 (FrameParser / UartChannel)
    asserts -- 断言与统计 (序号连续性 / 响应时间 / 上报周期)
    fixture -- pytest fixture (board)
"""

__all__ = ["qemu", "qmp", "uart", "asserts", "fixture"]
__version__ = "0.1.0"
