// The hourglass loader — drawn in CSS (styles.css, .hg-*), no package needed.
// Size, colour and speed follow the --uib-* properties it was designed with.
import React, { useEffect, useRef, useState } from 'react'

export function Hourglass({ size = 40, color, speed = 1.75, bgOpacity = 0.1, label = 'Loading' }) {
  const style = { '--uib-size': `${size}px`, '--uib-speed': `${speed}s`, '--uib-bg-opacity': bgOpacity }
  if (color) style['--uib-color'] = color
  return (
    <span className="hg" style={style} role="status" aria-label={label}>
      <span className="hg-half" /><span className="hg-half" />
    </span>
  )
}

// A loading state: the hourglass, with what is being waited for under it.
// `inline` puts the two side by side, for a small space in a panel.
export function Loading({ children = 'Loading…', size, inline, style }) {
  return (
    <div className={'hg-loading' + (inline ? ' inline' : '')} style={style}>
      <Hourglass size={size || (inline ? 22 : 40)} label={typeof children === 'string' ? children : 'Loading'} />
      {children && <div>{children}</div>}
    </div>
  )
}

// Whether a same-origin frame is between pages: from the moment its page starts
// to unload until the next one has loaded. Shown only after `delay` ms, so a
// page that comes back at once does not flash the hourglass.
export function useFrameLoading(frame, key, delay = 250) {
  const [busy, setBusy] = useState(true)
  const [shown, setShown] = useState(false)
  const timer = useRef(null)
  useEffect(() => { setBusy(true) }, [key])
  useEffect(() => {
    clearTimeout(timer.current)
    if (busy) timer.current = setTimeout(() => setShown(true), delay)
    else setShown(false)
    return () => clearTimeout(timer.current)
  }, [busy, delay])
  const onLoad = () => {
    setBusy(false)
    try {   // the Store is ours, so its next navigation can be seen starting
      frame.current?.contentWindow?.addEventListener('beforeunload', () => setBusy(true))
    } catch { /* another origin — only the first load is known */ }
  }
  return [shown, onLoad]
}
