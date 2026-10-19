from . import conf


def panel(request):
    return {"site_name": conf.get("site.name"), "default_theme": conf.get("ui.theme"), "poll_ms": conf.get("ui.poll_interval_ms")}
