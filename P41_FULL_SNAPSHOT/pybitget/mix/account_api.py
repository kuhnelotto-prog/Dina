"""
Stub for pybitget.mix.account_api to satisfy import errors.
The actual implementation is provided by the python-bitget package.
"""

from typing import Any

class AccountApi:
    def __init__(self, client: Any) -> None:
        self._client = client
    
    def get_account_info(self, *args, **kwargs) -> Any:
        pass
    
    def get_balance(self, *args, **kwargs) -> Any:
        pass
    
    def get_bills(self, *args, **kwargs) -> Any:
        pass
    
    def get_max_withdraw(self, *args, **kwargs) -> Any:
        pass
    
    def get_leverage_info(self, *args, **kwargs) -> Any:
        pass