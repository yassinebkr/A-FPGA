import { useState, useRef, useEffect } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Square } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { parseToolResult } from '@/lib/toolResult'
import { contractGate } from '@/lib/designContract'
import DecisionTimeline from './DecisionTimeline'
import DesignContract from './DesignContract'

function resultSummary(content) {
  const text = String(content ?? '')
  const parsed = parseToolResult(text)
  if (parsed?.verdict) return parsed.verdict
  if (parsed?.ok === true) return 'Checks passed'
  if (parsed?.ok === false) return parsed.errors?.[0]?.msg || 'Checks failed'
  if (Number(parsed?.errors_total) > 0) return String(parsed.errors_total) + ' errors'
  return text.split("\n").map((line) => line.trim()).find(Boolean) || 'Tool completed'
}

function usageSummary(usage) {
  if (!usage || typeof usage !== 'object') return null
  const total = usage.total_tokens ?? usage.totalTokens
  const output = usage.output_tokens ?? usage.completion_tokens ?? usage.outputTokens
  const reasoning = usage.reasoning_tokens ?? usage.reasoningTokens
  if (total !== undefined) return `${Number(total).toLocaleString()} tokens`
  if (output !== undefined || reasoning !== undefined) {
    const parts = []
    if (output !== undefined) parts.push(`${Number(output).toLocaleString()} output`)
    if (reasoning !== undefined) parts.push(`${Number(reasoning).toLocaleString()} reasoning`)
    return parts.join(' · ')
  }
  return null
}

function WorkingTile({ reasoning = {}, generation = {} }) {
  const [pinned, setPinned] = useState(false)
  const [hovered, setHovered] = useState(false)
  const [hoverDismissed, setHoverDismissed] = useState(false)
  const [panelMaxHeight, setPanelMaxHeight] = useState(448)
  const [openUp, setOpenUp] = useState(false)
  const shellRef = useRef(null)
  const contentRef = useRef(null)
  const contentFollowingRef = useRef(true)
  const active = Boolean(reasoning.active || generation.active)
  const open = pinned || (hovered && !hoverDismissed)

  const content = String(reasoning.content || '')
  const usage = usageSummary(reasoning.usage || generation.usage)

  // A completed generation always renders the compact tile. The panel can
  // still be opened deliberately, but it cannot remain open accidentally
  // after the provider closes the stream.
  const visibleOpen = open

  useEffect(() => {
    if (!visibleOpen) return undefined
    const dismissOutside = (event) => {
      const target = event.target
      if (!shellRef.current?.contains(target)) {
        setPinned(false)
        setHovered(false)
        setHoverDismissed(true)
      }
    }
    document.addEventListener('pointerdown', dismissOutside, true)
    const dismissEscape = (event) => {
      if (event.key === 'Escape') {
        setPinned(false)
        setHovered(false)
        setHoverDismissed(true)
      }
    }
    document.addEventListener('keydown', dismissEscape)
    return () => {
      document.removeEventListener('pointerdown', dismissOutside, true)
      document.removeEventListener('keydown', dismissEscape)
    }
  }, [visibleOpen])

  useEffect(() => {
    const element = contentRef.current
    if (!element || !contentFollowingRef.current) return
    element.scrollTop = element.scrollHeight
  }, [content, visibleOpen])

  useEffect(() => {
    if (!visibleOpen) return undefined
    const shell = shellRef.current
    const viewport = shell?.closest('[data-agent-scroll-region]')
    if (!shell || !viewport) return undefined
    const fitPanel = () => {
      const shellBox = shell.getBoundingClientRect()
      const regionBox = viewport.getBoundingClientRect()
      const viewportTop = Math.max(regionBox.top, 0)
      const viewportBottom = Math.min(
        regionBox.bottom,
        window.innerHeight,
      )
      // getBoundingClientRect() reflects CSS zoom while a pixel max-height is
      // zoomed again. Convert the measured room back to CSS pixels.
      const scale = shell.offsetHeight > 0 ? shellBox.height / shell.offsetHeight : 1
      const roomBelow = (viewportBottom - shellBox.bottom - 8) / Math.max(scale, 0.1)
      const roomAbove = (shellBox.top - viewportTop - 8) / Math.max(scale, 0.1)
      // The tile sits at the bottom of the transcript, right above the
      // composer: opening downward there pushes the panel off-screen and
      // the visible sliver shows only its header. Anchor the panel toward
      // whichever side has more room, and clamp its height to that side.
      const upward = roomAbove > roomBelow
      setOpenUp(upward)
      setPanelMaxHeight(Math.max(32, Math.min(448, upward ? roomAbove : roomBelow)))
    }
    fitPanel()
    window.addEventListener('resize', fitPanel)
    // Auto-scroll keeps the transcript pinned to the bottom, so the tile
    // moves under the pointer while the panel is open; re-fit on region scroll.
    viewport.addEventListener('scroll', fitPanel, { passive: true })
    return () => {
      window.removeEventListener('resize', fitPanel)
      viewport.removeEventListener('scroll', fitPanel)
    }
  }, [visibleOpen])

  if (!active && !content && !usage) return null

  const round = reasoning.round ?? generation.round
  const preview = content.trim().split('\n').map((line) => line.trim()).filter(Boolean).at(-1) || (active ? 'Generating response…' : 'Generation complete')
  const label = active ? 'Working' : 'Reasoning complete'

  return (
    <div
      ref={shellRef}
      className="relative flex flex-col items-start self-start"
      onMouseEnter={() => {
        setHovered(true)
        setHoverDismissed(false)
      }}
      onMouseLeave={() => setHovered(false)}
    >
      <button
        type="button"
        aria-label={visibleOpen ? 'Hide model reasoning' : 'Show model reasoning'}
        aria-expanded={visibleOpen}
        onClick={() => {
          if (pinned) {
            setPinned(false)
            setHovered(false)
            setHoverDismissed(true)
          } else {
            setHoverDismissed(false)
            setPinned(true)
          }
        }}
        className="group flex min-h-7 items-center gap-2 rounded-md border border-transparent px-2 py-1 text-left transition-colors hover:border-gray-800 focus-visible:border-gray-600 focus-visible:outline-none"
      >
        <span className={'h-2 w-2 shrink-0 rounded-full bg-amber-400 transition-transform duration-200 group-hover:scale-150' + (active ? ' animate-pulse' : '')} />
        <span className="text-[10px] font-mono uppercase tracking-[.14em] text-gray-500">{label}</span>
        {round !== null && <span className="text-[10px] font-mono text-gray-700">round {round}</span>}
        {usage && <span className="text-[10px] font-mono text-gray-700">{usage}</span>}
      </button>

      {visibleOpen && (
        <section
          role="region"
          aria-label="Full model reasoning"
          className={'absolute left-0 z-40 w-[min(32rem,calc(100vw-3rem))] overflow-y-auto rounded-lg border border-gray-700/90 bg-[#101010]/[.98] p-3 shadow-2xl shadow-black/50 backdrop-blur-sm ' + (openUp ? 'bottom-full mb-1' : 'top-full mt-1')}
          style={{ maxHeight: `${panelMaxHeight}px` }}
          onMouseEnter={() => setHovered(true)}
        >
          <div className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2">
              <span className={'h-2 w-2 shrink-0 rounded-full bg-amber-400' + (active ? ' animate-pulse' : '')} />
              <span className="text-[9px] font-mono uppercase tracking-[.16em] text-gray-500">{label}</span>
            </div>
            <div className="shrink-0 text-[9px] font-mono text-gray-600">
              {round !== null && `round ${round}`}{round !== null && usage && ' · '}{usage}
            </div>
          </div>
          <div className="mt-2 text-[10px] font-mono text-gray-600">{preview}</div>
          {pinned && content && (
            <pre
            ref={contentRef}
            onScroll={(event) => {
              const element = event.currentTarget
              contentFollowingRef.current = element.scrollHeight - element.scrollTop - element.clientHeight <= 24
            }}
            className="mt-2 max-h-[min(24rem,calc(100vh-18rem))] overflow-y-auto whitespace-pre-wrap break-words border-t border-gray-800 pt-2 text-[10px] leading-relaxed text-gray-400"
          >{content}</pre>
          )}
          <div className="mt-2 text-[9px] font-mono text-gray-700">Click the dot to pin full reasoning.</div>
        </section>
      )}
    </div>
  )
}

function TimeStamp({ ts }) {
  if (!ts) return null
  const date = new Date(Number(ts) * (Number(ts) > 1e12 ? 1 : 1000))
  if (!Number.isFinite(date.getTime())) return null
  return (
    <span className="shrink-0 text-[8px] font-mono text-gray-700" title="frame time">
      {date.toLocaleTimeString([], { hour12: false })}
    </span>
  )
}

function Message({ msg }) {
  if (msg.role === 'user') {
    return (
      <div className="flex items-center justify-end gap-1.5">
        <TimeStamp ts={msg.ts} />
        <div className="max-w-[85%] rounded-md bg-emerald-500/10 border border-emerald-500/30 px-3 py-2 text-sm text-emerald-100">
          <span className="text-emerald-500 font-mono mr-2">❯</span>{msg.content}
        </div>
      </div>
    )
  }
  if (msg.role === 'tool_call') {
    return (
      <div className="flex items-center gap-2 rounded-md border border-gray-800 bg-[#111111] px-2.5 py-1.5 font-mono text-xs text-gray-400">
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-gray-500" />
        <span className="text-gray-600">call</span>
        <span className="text-gray-300">{msg.content}</span>
        <TimeStamp ts={msg.ts} />
      </div>
    )
  }
  if (msg.role === 'tool_result') {
    const summary = resultSummary(msg.content)
    return (
      <details className="group rounded-md border border-gray-800 bg-[#111111] font-mono text-xs">
        <summary className="flex cursor-pointer list-none items-center gap-2 px-2.5 py-1.5 text-gray-400 [&::-webkit-details-marker]:hidden">
          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${msg.ok ? 'bg-emerald-500' : 'bg-red-500'}`} />
          <span className="text-gray-600">result</span>
          <span className={msg.ok ? 'text-gray-300' : 'text-red-300'}>{summary}</span>
          <span className="ml-auto text-[10px] text-gray-700 group-open:rotate-90">›</span>
          <TimeStamp ts={msg.ts} />
        </summary>
        <pre className="max-h-56 overflow-y-auto whitespace-pre-wrap break-words border-t border-gray-800 px-2.5 py-2 text-gray-400">{msg.content}</pre>
      </details>
    )
  }
  if (msg.role === 'error') {
    return (
      <div className="rounded-md bg-red-500/10 border border-red-500/40 px-3 py-2 text-sm text-red-300 font-mono">
        {msg.content}
      </div>
    )
  }
  return (
    <div className="flex justify-start">
      <div className="max-w-[85%] rounded-md bg-[#161616] border border-gray-800 px-3 py-2 text-sm text-gray-200 agent-md">
        <Markdown remarkPlugins={[remarkGfm]}>{msg.content}</Markdown>
      </div>
    </div>
  )
}

export default function AgentChat({ messages, busy, isRunning, statusText, runState, generation, reasoning, timeline, designContract, metrics, contractActionPending, onContractAction, connected, onSend, onCancel }) {
  const [input, setInput] = useState('')
  const scrollRef = useRef(null)
  const autoScrollRef = useRef(true)
  const contractStatus = contractGate(designContract, { busy, isRunning })
  const inputBlocked = Boolean(contractStatus.blocked || contractActionPending)

  const scrollToBottom = (behavior = "auto") => {
    const element = scrollRef.current
    if (!element) return
    element.scrollTo({ top: element.scrollHeight, behavior })
  }

  useEffect(() => {
    if (!autoScrollRef.current) return undefined
    // Wait until streamed markdown/reasoning has been laid out before measuring
    // the new height. `auto` keeps the viewport pinned during rapid deltas.
    const frame = requestAnimationFrame(() => scrollToBottom())
    return () => cancelAnimationFrame(frame)
  }, [messages, busy, generation, reasoning, statusText])

  const handleScroll = () => {
    const element = scrollRef.current
    if (!element) return
    const distanceFromBottom = element.scrollHeight - element.scrollTop - element.clientHeight
    autoScrollRef.current = distanceFromBottom <= 32
  }

  const sendMessage = () => {
    if (!input.trim() || !connected || inputBlocked) return
    // A new run explicitly resumes following the conversation.
    autoScrollRef.current = true
    if (onSend(input) !== false) setInput('')
  }

  return (
    <div className="bg-[#0d0d0d] rounded-lg border border-gray-800 flex flex-col h-full overflow-hidden">
      <div className="relative px-4 py-2.5 border-b border-gray-800 flex items-center justify-between bg-[#111111]">
        <h2 className="text-xs font-mono uppercase tracking-wider text-gray-400">Agent Console</h2>
        <DecisionTimeline events={timeline} busy={busy} statusText={statusText} runState={runState} />
      </div>

      <DesignContract contract={designContract} metrics={metrics} busy={busy} isRunning={isRunning} onAction={onContractAction} />

      <div ref={scrollRef} data-agent-scroll-region onScroll={handleScroll} className="flex-grow p-4 overflow-y-auto space-y-3">
        {messages.map((msg, idx) => <Message key={msg.streamId || idx} msg={msg} />)}
        <WorkingTile
          key={`${generation?.generationId || 'generation'}-${generation?.active ? 'active' : 'complete'}`}
          reasoning={reasoning}
          generation={generation}
        />
        {busy && !generation?.active && !reasoning?.active && (
          <div className="font-mono text-xs text-gray-500 animate-pulse">{statusText || 'agent working…'}</div>
        )}
      </div>

      <div className="p-3 border-t border-gray-800 bg-[#111111]">
        <div className="flex gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && sendMessage()}
            placeholder={!connected ? 'Backend offline — retrying…' : inputBlocked && contractStatus.reason ? contractStatus.reason : 'Describe the hardware to build…'}
            disabled={!connected || inputBlocked}
            className="flex-grow bg-[#0a0a0a] border border-gray-800 rounded-md px-3 py-2 text-sm font-mono text-gray-200 placeholder-gray-600 focus:outline-none focus:border-emerald-600/60 disabled:opacity-50 transition-colors"
          />
          {isRunning ? (
            <Button
              onClick={onCancel}
              disabled={!connected}
              variant="destructive"
              size="icon"
              title="Stop run"
              className="rounded-md disabled:opacity-40"
            >
              <Square className="w-4 h-4 fill-current" />
            </Button>
          ) : (
            <Button
              onClick={sendMessage}
              disabled={!connected || busy || inputBlocked}
              className="bg-emerald-600 hover:bg-emerald-500 text-white rounded-md px-5 text-sm font-medium disabled:opacity-40"
            >
              Run
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}
