"""成本路由：单个知识库的累计整理成本。

从 `routers/kbs.py` 按职责拆出（P1-4）。本模块不反向 import `kbs.py`；
`kbs.py` 聚合本模块的 router 并重导出 `kb_cost`。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_user, grant_level
from ..services import cost

router = APIRouter(tags=["knowledge-base"])


@router.get("/kbs/{kb_id}/cost")
def kb_cost(kb_id: int, db: Session = Depends(get_db),
            user: models.User = Depends(get_current_user)):
    """该知识库的累计整理成本（模型调用量 / token / 耗时）。

    权限：沿用 grant_level，read 即可查看——成本是「这个库整理花了多少」的
    说明性数据，不是管理动作；但看不到库的人也不该看到它的成本构成，
    否则可以借此推断别部门的知识库规模。

    为什么不做成 admin-only：论文实验用的是普通员工账号跑问答，
    如果只有管理员能读成本，采集脚本就得持管理员凭据，
    权限模型会被实验流程倒逼放宽，这是更差的结果。
    """
    kb = db.get(models.KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    level = grant_level(db, user, kb_id)
    if level is None:
        raise HTTPException(status_code=403, detail="没有该知识库的访问权限")
    data = cost.kb_summary(db, kb)
    data["my_level"] = level
    return data
