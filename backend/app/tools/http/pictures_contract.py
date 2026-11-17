"""图片抓取工具的公开输入，不引入网络运行时。"""

from pydantic import BaseModel, ConfigDict, Field


class FetchPicture(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(
        min_length=1, max_length=2000, description="HTTPS 图片直链或含 img 标签的静态网页地址"
    )
    max_images: int = Field(default=3, ge=1, le=6)
    as_zip: bool = Field(default=False, description="将抓取到的图片一起打包为 ZIP 附件")
