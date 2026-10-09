import { useEffect, useMemo, useState } from 'react'
import { ChevronDown, ChevronRight, FileCode2, Search } from 'lucide-react'

const KEYWORDS = new Set([
  'module', 'endmodule', 'input', 'output', 'inout', 'wire', 'reg', 'logic',
  'assign', 'always', 'always_ff', 'always_comb', 'if', 'else', 'begin', 'end',
  'case', 'casez', 'casex', 'endcase', 'default', 'for', 'while', 'generate',
  'endgenerate', 'function', 'endfunction', 'task', 'endtask', 'parameter',
  'localparam', 'genvar', 'posedge', 'negedge', 'initial', 'signed', 'unsigned',
  'integer', 'timescale', 'package', 'endpackage', 'interface', 'endinterface',
])
const GROUPS = [['rtl', 'RTL'], ['package', 'Packages'], ['header', 'Headers'], ['testbench', 'Testbenches'], ['constraint', 'Constraints']]

function renderLine(line, key) {
  const commentIdx = line.indexOf('//')
  const code = commentIdx >= 0 ? line.slice(0, commentIdx) : line
  const comment = commentIdx >= 0 ? line.slice(commentIdx) : null
  const tokens = code.split(/(\b\w+\b|`\w+|\d+'[bdh][0-9a-fA-F_xzXZ]+|\b\d+\b)/g)
  return <div key={key} id={`srcline-${key + 1}`} className="flex"><span className="w-10 shrink-0 select-none text-right pr-3 text-gray-700">{key + 1}</span><span className="whitespace-pre">{tokens.map((tok, i) => {
    let cls = 'text-gray-300'
    if (!tok) return null
    if (KEYWORDS.has(tok)) cls = 'text-fuchsia-400'
    else if (tok.startsWith('`')) cls = 'text-amber-400'
    else if (/^\d/.test(tok)) cls = 'text-cyan-300'
    return <span key={i} className={cls}>{tok}</span>
  })}{comment && <span className="text-gray-600">{comment}</span>}</span></div>
}

function shortSize(size) {
  const value = Number(size)
  if (!Number.isFinite(value)) return 'unknown size'
  if (value < 1024) return `${value} B`
  return `${(value / 1024).toFixed(value < 10240 ? 1 : 0)} KB`
}

function SourceGroup({ label, files, selectedPath, onSelect, compileIndex, topModule }) {
  const [open, setOpen] = useState(true)
  if (!files.length) return null
  return <section data-testid={`source-group-${label.toLowerCase()}`}>
    <button type="button" onClick={() => setOpen((value) => !value)} className="flex w-full items-center gap-1.5 border-b border-gray-900 px-3 py-1.5 text-left font-mono text-[9px] uppercase tracking-wider text-gray-500 hover:bg-white/[0.02] hover:text-gray-300" aria-expanded={open}>
      {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}<span>{label}</span><span className="ml-auto text-gray-700">{files.length}</span>
    </button>
    {open && files.map((file) => {
      const order = compileIndex.get(file.path)
      const declaresTop = file.content?.match(new RegExp(`\\bmodule\\s+${topModule}\\b`))
      return <button key={file.path} type="button" onClick={() => onSelect(file.path)} className={`block w-full border-b border-gray-900 px-3 py-2 text-left font-mono transition-colors ${selectedPath === file.path ? 'bg-emerald-500/5 text-gray-100' : 'text-gray-500 hover:bg-white/[0.02] hover:text-gray-300'}`} title={file.path}>
        <span className="flex min-w-0 items-center gap-1.5"><FileCode2 className="h-3 w-3 shrink-0 text-gray-600" /><span className="truncate text-[10px]">{file.path}</span></span>
        <span className="mt-1 flex items-center gap-1 text-[8px] uppercase tracking-wider text-gray-700">{Number.isInteger(order) && <span className="rounded border border-gray-800 px-1">#{order + 1}</span>}{declaresTop && <span className="rounded border border-violet-500/30 px-1 text-violet-400">top</span>}<span>{file.language}</span></span>
      </button>
    })}
  </section>
}

function SourceWorkspace({ verilog, hardwareProject, focusSource }) {
  const files = useMemo(() => Array.isArray(hardwareProject?.files) ? hardwareProject.files : [], [hardwareProject])
  const digest = hardwareProject?.digest ?? null
  const [requestedPath, setRequestedPath] = useState(null)
  const [query, setQuery] = useState('')
  const [role, setRole] = useState('all')
  const compileIndex = useMemo(() => new Map((hardwareProject?.compile_order ?? []).map((path, index) => [path, index])), [hardwareProject])
  const selected = files.find((file) => file.path === requestedPath) ?? files[0]
  // Cross-probing from the schematic (Phase G2): a source attribute like
  // "counter8.v:12" selects the matching manifest file by path or basename.
  useEffect(() => {
    if (!focusSource?.file) return
    const wanted = focusSource.file
    const match = files.find((file) => file.path === wanted)
      ?? files.find((file) => file.path.endsWith(`/${wanted}`) || wanted.endsWith(`/${file.path}`))
      ?? files.find((file) => file.path.split('/').pop() === wanted.split('/').pop())
    if (match) setRequestedPath(match.path)
  }, [focusSource, files])
  // Scroll the code pane to the referenced line once the file is shown.
  useEffect(() => {
    if (!focusSource?.line || !selected) return
    const element = document.getElementById(`srcline-${focusSource.line}`)
    element?.scrollIntoView({ block: 'center' })
  }, [focusSource, selected])
  const normalizedQuery = query.trim().toLowerCase()
  const filtered = files.filter((file) => (role === 'all' || file.role === role) && (!normalizedQuery || file.path.toLowerCase().includes(normalizedQuery)))
  const grouped = new Map(GROUPS.map(([key]) => [key, []]))
  for (const file of filtered) { if (!grouped.has(file.role)) grouped.set(file.role, []); grouped.get(file.role).push(file) }
  const source = selected ? { name: selected.path, code: selected.content ?? '' } : verilog ? { name: `${verilog.top}.v`, code: verilog.code } : null
  const provenance = hardwareProject?.provenance ?? {}
  const roleCounts = files.reduce((counts, file) => { counts[file.role] = (counts[file.role] ?? 0) + 1; return counts }, {})

  if (!source) return <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center font-mono"><div className="text-sm text-gray-500">No source set yet</div><div className="max-w-md text-[10px] leading-relaxed text-gray-700">A generated or imported hardware-project manifest will expose its RTL, packages, testbenches, and constraints here.</div></div>

  if (!files.length) return <div className="flex h-full min-h-0 flex-col" data-testid="legacy-source-view">
    <div className="border-b border-gray-800 px-3 py-2 font-mono"><div className="flex items-center justify-between gap-3"><span className="truncate text-xs text-gray-300">{source.name}</span><span className="rounded border border-amber-500/30 px-1.5 py-0.5 text-[8px] uppercase tracking-wider text-amber-400">legacy snapshot</span></div><div className="mt-1 text-[9px] text-gray-600">No authoritative multi-file manifest is attached to this project.</div></div>
    <div className="flex-grow overflow-auto p-3 font-mono text-xs leading-5">{source.code.split("\n").map((line, i) => renderLine(line, i))}</div>
  </div>

  return <div className="flex h-full min-h-0" data-testid="sources-view">
    <aside className="flex w-64 shrink-0 flex-col border-r border-gray-800 bg-[#0d0d0d]">
      <div className="border-b border-gray-800 px-3 py-2.5 font-mono">
        <div className="flex items-center gap-2"><span className="truncate text-[11px] text-gray-200">{hardwareProject.top_module}</span><span className="ml-auto text-[9px] text-gray-600">{files.length} files</span></div>
        <div className="mt-1 flex flex-wrap gap-x-2 text-[8px] uppercase tracking-wider text-gray-600"><span>rev {hardwareProject.revision}</span><span title={digest}>{digest?.slice(0, 10)}</span><span>{compileIndex.size} compiled</span></div>
        {(provenance.name || provenance.commit || provenance.upstream) && <div className="mt-2 truncate border-t border-gray-800 pt-2 text-[9px] text-gray-500" title={provenance.upstream}>{provenance.name ?? provenance.upstream ?? 'Imported source'}{provenance.commit && <span className="text-gray-700"> | {provenance.commit.slice(0, 10)}</span>}</div>}
      </div>
      <div className="border-b border-gray-800 p-2">
        <label className="flex items-center gap-1.5 rounded border border-gray-800 bg-[#090909] px-2"><Search className="h-3 w-3 text-gray-600" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter source paths" aria-label="Filter source paths" className="min-w-0 flex-1 bg-transparent py-1.5 font-mono text-[10px] text-gray-300 outline-none placeholder:text-gray-700" /></label>
        <div className="mt-2 flex flex-wrap gap-1">{['all', ...GROUPS.map(([key]) => key)].map((key) => <button key={key} type="button" onClick={() => setRole(key)} className={`rounded border px-1.5 py-0.5 font-mono text-[8px] uppercase tracking-wider ${role === key ? 'border-gray-600 text-gray-200' : 'border-gray-800 text-gray-600 hover:text-gray-400'}`}>{key === 'all' ? 'all' : key}{key !== 'all' && roleCounts[key] ? ` ${roleCounts[key]}` : ''}</button>)}</div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {GROUPS.map(([key, label]) => <SourceGroup key={key} label={label} files={grouped.get(key) ?? []} selectedPath={selected?.path} onSelect={setRequestedPath} compileIndex={compileIndex} topModule={hardwareProject.top_module} />)}
        {[...grouped.entries()].filter(([key]) => !GROUPS.some(([known]) => known === key)).map(([key, entries]) => <SourceGroup key={key} label={key} files={entries} selectedPath={selected?.path} onSelect={setRequestedPath} compileIndex={compileIndex} topModule={hardwareProject.top_module} />)}
        {!filtered.length && <div className="px-3 py-4 text-center font-mono text-[10px] text-gray-700">No matching sources</div>}
      </div>
    </aside>
    <div className="flex min-w-0 flex-1 flex-col">
      <div className="border-b border-gray-800 px-3 py-2 font-mono">
        <div className="flex items-center justify-between gap-3"><span className="truncate text-xs text-gray-300">{source.name}</span><span className="shrink-0 text-[9px] text-gray-600">{source.code.split('\n').length} lines | {shortSize(selected?.size)}</span></div>
        <div className="mt-1 flex min-w-0 items-center gap-2 text-[8px] uppercase tracking-wider text-gray-600"><span>{selected?.role}</span><span>{selected?.language}</span>{compileIndex.has(selected?.path) && <span>compile #{compileIndex.get(selected.path) + 1}</span>}<span className="ml-auto truncate normal-case tracking-normal" title={selected?.sha256}>sha256 {selected?.sha256?.slice(0, 12)}</span></div>
      </div>
      <div className="flex-grow overflow-auto p-3 font-mono text-xs leading-5">{source.code.split("\n").map((line, i) => renderLine(line, i))}</div>
    </div>
  </div>
}

export default function VerilogView(props) {
  const identity = props.hardwareProject?.digest ?? `legacy:${props.verilog?.top ?? 'empty'}`
  return <SourceWorkspace key={identity} {...props} />
}
