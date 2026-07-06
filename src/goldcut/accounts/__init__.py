"""Accounts — идентичность, план, резолв рендер-профиля.

Тонкий слой над Store: жизненный цикл пользователя + правила, как из настроек
аккаунта и разового оверрайда из чата получить итоговый RenderProfile.
"""

from __future__ import annotations

from goldcut.models import Account, RenderProfile, Request
from goldcut.store import Store


def get_or_create(store: Store, user_id: int, username: str | None, locale: str | None) -> Account:
    return store.get_or_create_user(user_id, username, locale)


def resolve_profile(account: Account, request: Request) -> RenderProfile:
    """Итоговый профиль рендера: настройки аккаунта → оверрайд из чата.

    Дефолт — faithful trim (mode='trim' в настройках). Вертикальный шортс (9:16)
    только если юзер сам его выбрал в mini-app или попросил в чате. Никаких
    скрытых фича-дефолтов — что в настройках, то и режем.
    """
    p = account.settings.model_copy()

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
