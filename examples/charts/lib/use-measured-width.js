import { useEffect, useRef, useState } from 'react';

/**
 * Track an element's width, measured after it mounts.
 *
 * Recharts ships `<ResponsiveContainer>`, which measures the DOM to size the
 * chart. There is no DOM during server rendering, so it emits an empty `<div>`
 * and the chart only appears once JavaScript has run. Passing an explicit
 * `width` to the chart instead makes Recharts render real SVG on the server —
 * axes, gridlines and the plotted path are all in the HTML.
 *
 * This hook keeps that server render *and* stays responsive: `fallback` is the
 * width used on the server and for the first client render, so the two agree
 * and hydration stays clean, and the measured width takes over immediately
 * afterwards.
 *
 * @param {number} fallback Width used on the server and the first client render.
 * @returns {[React.RefObject<HTMLElement>, number]} A ref to attach to the
 *   chart's container, and the width to hand the chart.
 */
export function useMeasuredWidth(fallback) {
  const ref = useRef(null);
  const [width, setWidth] = useState(fallback);

  useEffect(() => {
    const node = ref.current;
    if (!node || typeof ResizeObserver === 'undefined') {
      return undefined;
    }
    const observer = new ResizeObserver(([entry]) => {
      setWidth(Math.max(280, Math.round(entry.contentRect.width)));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return [ref, width];
}
