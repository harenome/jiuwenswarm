# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Implementations of the typed-decision protocol, one module each.

An adapter turns a state and a set of questions into typed answers. Everything
that is true of one implementation and not of another belongs inside one of
these modules: the endpoint path, the request envelope, the vendor quirks, and
which modalities the state may hold.

**None of their differences may reach the caller.** The caller asks a question
and reads an answer. Which adapter answered is a configuration matter.
"""

from __future__ import annotations

from jiuwenswarm.common.typed_decision.adapters.base import (
    DecisionAdapter,
    TEXT_ONLY,
)
from jiuwenswarm.common.typed_decision.adapters.jev import JevAdapter

__all__ = ["DecisionAdapter", "JevAdapter", "TEXT_ONLY"]
