"""Accounts — идентичность, план, резолв рендер-профиля.

Тонкий слой над Store: жизненный цикл пользователя + правила, как из настроек
аккаунта и разового оверрайда из чата получить итоговый RenderProfile.
"""

from __future__ import annotations

from goldcut.models import Account, RenderProfile, Request
from goldcut.store import Store

# Дефолт формата на фичу, если у юзера дефолтные (не тронутые) настройки.
# F1 «дай кусок» → faithful trim; F2 «сделай клипы» → вертикальный шортс.
FEATURE_DEFAULT_MODE = {"locate": "trim", "curate": "short"}


def get_or_create(store: Store, user_id: int, username: str | None, locale: str | None) -> Account:
    return store.get_or_create_user(user_id, username, locale)


def resolve_profile(account: Account, request: Request) -> RenderProfile:
    """Итоговый профиль рендера: настройки аккаунта → фича-дефолт → оверрайд из чата.

    Приоритет (по возрастанию): базовые настройки юзера < дефолт под фичу
    (только если юзер не менял mode) < явный оверрайд из реплики.
    """
    p = account.settings.model_copy()

    # фича-дефолт mode применяем, только если юзер оставил дефолтный 'trim'
    # (т.е. не выражал предпочтения) — иначе уважаем его выбор
    if account.settings.mode == "trim" and request.mode in FEATURE_DEFAULT_MODE:
        p.mode = FEATURE_DEFAULT_MODE[request.mode]

    if request.format_override:
        ov = request.format_override
        # оверрайдим только явно заданные поля (отличные от дефолтов модели)
        base = RenderProfile()
        if ov.mode != base.mode:
            p.mode = ov.mode
        if ov.aspect_ratio != base.aspect_ratio:
            p.aspect_ratio = ov.aspect_ratio
        if ov.subtitles != base.subtitles:
            p.subtitles = ov.subtitles

    return p
