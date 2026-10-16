"""Canonical frame windows for bounded media processing.

All bounds are integer 44.1 kHz frames on the complete source timeline and use
half-open ``[start_frame, end_frame)`` semantics.  Excerpt mode always uses one
frozen centered window; it never searches multiple windows or falls back.
"""

SAMPLE_RATE = 44100
MIN_SOURCE_FRAMES = 30 * SAMPLE_RATE
MAX_EXCERPT_SOURCE_FRAMES = 900 * SAMPLE_RATE
MAX_CONTEXT_FRAMES = 90 * SAMPLE_RATE


class ContextWindowError(ValueError):
    pass


def validate_source_frames(source_frames):
    if (type(source_frames) is not int or
            not MIN_SOURCE_FRAMES <= source_frames <= MAX_EXCERPT_SOURCE_FRAMES):
        raise ContextWindowError('Source length must be an integer 30–900 seconds')
    return source_frames


def freeze_context(source_frames):
    """Return the sole deterministic excerpt window on the source timeline."""
    validate_source_frames(source_frames)
    window_frames = min(MAX_CONTEXT_FRAMES, source_frames)
    start_frame = (source_frames - window_frames) // 2
    return {'start_frame':start_frame, 'end_frame':start_frame + window_frames}


def full_context(source_frames):
    """Describe a full render using the same context-window representation."""
    validate_source_frames(source_frames)
    return {'start_frame':0, 'end_frame':source_frames}


def validate_context_window(context_window, source_frames, *, frozen=False):
    """Validate an embedded context against its complete source length."""
    validate_source_frames(source_frames)
    if (not isinstance(context_window, dict) or
            set(context_window) != {'start_frame','end_frame'} or
            any(type(context_window[name]) is not int for name in context_window)):
        raise ContextWindowError('Invalid context window schema')
    start = context_window['start_frame']
    end = context_window['end_frame']
    if not 0 <= start < end <= source_frames:
        raise ContextWindowError('Context window is outside the source timeline')
    expected = freeze_context(source_frames) if frozen else full_context(source_frames)
    if context_window != expected:
        raise ContextWindowError('Context window differs from the frozen policy')
    return context_window
