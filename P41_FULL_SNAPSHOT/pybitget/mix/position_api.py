"""
Stub for pybitget.mix.position_api to satisfy import errors.
The actual implementation is provided by the python-bitget package.
"""

from typing import Any

class PositionApi:
    def __init__(self, client: Any) -> None:
        self._client = client
    
    def get_single_position(self, *args, **kwargs) -> Any:
        pass
    
    def get_all_positions(self, *args, **kwargs) -> Any:
        pass
    
    def allPosition(self, *args, **kwargs) -> Any:
        pass
    
    def modify_position(self, *args, **kwargs) -> Any:
        pass
    
    def set_leverage(self, *args, **kwargs) -> Any:
        pass
    
    def set_margin_mode(self, *args, **kwargs) -> Any:
        pass
