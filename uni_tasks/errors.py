class Problem(Exception):
    """Shared exception identity, including services launched with python -m."""
    def __init__(self,status,detail,code=None):
        self.status,self.detail,self.code=status,detail,code
        super().__init__(detail)
