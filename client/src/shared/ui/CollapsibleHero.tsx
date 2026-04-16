import { useState, type ReactNode } from 'react'

interface CollapsibleHeroProps {
  /** The section-tag label, e.g. "Stage 04" */
  tag: string
  /** Main heading shown in both collapsed and expanded states */
  title: string
  /** Status badge or route-badge shown inline when collapsed */
  statusSlot?: ReactNode
  /** Full hero body shown only when expanded */
  children: ReactNode
  /** Extra class names forwarded to the outer section */
  className?: string
  /** Start expanded (default false) */
  defaultExpanded?: boolean
}

export function CollapsibleHero({
  tag,
  title,
  statusSlot,
  children,
  className,
  defaultExpanded = true, // keep heroes open by default so main action buttons are visible
}: CollapsibleHeroProps) {
  const [expanded, setExpanded] = useState(defaultExpanded)

  return (
    <section className={`hero-panel hero-collapsible ${expanded ? 'is-expanded' : 'is-collapsed'} ${className || ''}`}>
      <button
        type="button"
        className="hero-collapse-toggle"
        onClick={() => setExpanded((prev) => !prev)}
        aria-expanded={expanded}
      >
        <div className="hero-collapse-summary">
          <span className="section-tag">{tag}</span>
          <h2>{title}</h2>
          {!expanded && statusSlot ? <div className="hero-collapse-status">{statusSlot}</div> : null}
        </div>
        <span className="hero-collapse-icon" aria-hidden="true">
          {expanded ? '▲' : '▼'}
        </span>
      </button>

      {expanded ? <div className="hero-collapse-body">{children}</div> : null}
    </section>
  )
}
