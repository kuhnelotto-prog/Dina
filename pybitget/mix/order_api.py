"""
Stub for pybitget.mix.order_api to satisfy import errors.
The actual implementation is provided by the python-bitget package.
"""

from typing import Any

class OrderApi:
    def __init__(self, client: Any) -> None:
        self._client = client
    
    def place_order(self, *args, **kwargs) -> Any:
        pass
    
    def placeOrder(self, *args, **kwargs) -> Any:
        pass
    
    def placePlanOrder(self, *args, **kwargs) -> Any:
        pass
    
    def ordersPlanPending(self, *args, **kwargs) -> Any:
        pass
    
    def cancelAllPlanOrders(self, *args, **kwargs) -> Any:
        pass
    
    def cancelPlanOrder(self, *args, **kwargs) -> Any:
        pass
    
    def detail(self, *args, **kwargs) -> Any:
        pass
    
    def cancel_order(self, *args, **kwargs) -> Any:
        pass
    
    def get_order_info(self, *args, **kwargs) -> Any:
        pass
    
    def get_open_orders(self, *args, **kwargs) -> Any:
        pass
    
    def batch_place_order(self, *args, **kwargs) -> Any:
        pass
    
    def batch_cancel_order(self, *args, **kwargs) -> Any:
        pass
