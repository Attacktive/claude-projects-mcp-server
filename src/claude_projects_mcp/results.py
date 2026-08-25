"""How a tool result carries a warning: first, and only when there is one."""


def with_warning(payload: dict, warning: str | None) -> dict:
	"""`payload` led by `warning` when there is one, and without the key when there is not.

	The model is the only reader a tool result is guaranteed to have, so the shape has to work on it: a key that reads null most of the time teaches it to skip the key, and a warning placed after the success fields arrives once everything already looks fine.
	"""
	if warning is None:
		return payload

	return {"warning": warning, **payload}
