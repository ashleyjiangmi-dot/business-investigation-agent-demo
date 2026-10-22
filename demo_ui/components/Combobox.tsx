'use client'

import { useEffect, useId, useRef, useState } from 'react'

type ComboboxProps = {
  /** 当前已提交的选项值（始终来自 options，保证提交合法） */
  value: string
  options: string[]
  onChange: (value: string) => void
  placeholder?: string
  required?: boolean
  /** 选项的展示文本映射；默认原样显示。提交值始终是 option 本身。 */
  labelFor?: (option: string) => string
  /** 只读选择模式：输入框不可打字，点击/聚焦直接展开列表选择（周期/场景等选项少的字段）。 */
  readOnly?: boolean
  /** 判定选项是否禁用（如两期下拉互斥）；禁用项置灰、不可点选，键盘导航跳过。 */
  isDisabled?: (option: string) => boolean
}

/**
 * 可输入过滤 + 下拉选择的组合框。
 * 交互口径：
 * - 聚焦或点展开按钮 → 清空输入框并列出**全部**选项（不把预填的已选值当过滤词，
 *   否则「全部商品…」会把其余项滤掉，逼用户先删字才能选）；高亮项默认落在当前已选项上。
 * - 打字 → 按子串实时过滤。
 * - 列表打开时 Enter 只负责选择有效匹配项：无匹配（或无高亮项）时既不提交表单、
 *   也不把旧已选项当结果回填——按键被吞掉，表单不触发。
 * - 点选 / 回车选中 → 提交该选项（值恒为下发 option 之一）。
 * - 失焦时输入恰好是合法选项 → 视为选择了它；否则回退到当前已选值，绝不提交非法值，
 *   与入口字段「必须在下发选项内」的服务端校验同一口径。
 * - Esc / 点箭头关闭 = 放弃本次筛选，不等于选择：输入框恢复当前已选项。
 * - readOnly 模式：不可打字，点击/聚焦直接展开全量列表；弹层锚定输入框下方同宽。
 * - isDisabled 判为禁用的选项置灰展示，不可点选、Enter 不选中，方向键导航自动跳过。
 */
export default function Combobox({
  value,
  options,
  onChange,
  placeholder,
  required,
  labelFor = (o) => o,
  readOnly = false,
  isDisabled,
}: ComboboxProps) {
  const listId = useId()
  const inputRef = useRef<HTMLInputElement>(null)
  const [text, setText] = useState(() => labelFor(value))
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)

  // 外部 value 变化（字段目录加载 / 选项回填）时同步回显；用户正在打字时 value 不变，不打断
  useEffect(() => {
    setText(labelFor(value))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])

  // readOnly 模式没有打字输入，输入框里是回显标签，不能当过滤词用
  const query = readOnly ? '' : text.trim().toLowerCase()
  const matches = query ? options.filter((o) => labelFor(o).toLowerCase().includes(query)) : options

  // 过滤结果变化后把高亮项夹回有效范围
  useEffect(() => {
    if (active >= matches.length) setActive(0)
  }, [matches.length, active])

  /** 打开列表时的默认高亮：落在当前已选项；已选项被禁用则取第一个可用项。 */
  function initialActiveIndex() {
    const idx = Math.max(0, options.indexOf(value))
    if (!isDisabled?.(options[idx])) return idx
    const firstEnabled = options.findIndex((o) => !isDisabled?.(o))
    return firstEnabled >= 0 ? firstEnabled : idx
  }

  /** 方向键移动：跳过禁用项；整列都禁用时停在原地。 */
  function moveActive(dir: 1 | -1) {
    setActive((a) => {
      for (let i = a + dir; i >= 0 && i < matches.length; i += dir) {
        if (!isDisabled?.(matches[i])) return i
      }
      return a
    })
  }

  function showAll() {
    if (!readOnly) setText('')
    // 高亮默认落在当前已选项上：聚焦后直接回车=重选当前值，不会误跳到列表第一项
    setActive(initialActiveIndex())
    setOpen(true)
    requestAnimationFrame(() => inputRef.current?.focus())
  }

  function select(option: string) {
    if (isDisabled?.(option)) return
    onChange(option)
    setText(labelFor(option))
    setOpen(false)
  }

  function revert() {
    setText(labelFor(value))
    setOpen(false)
  }

  function commitOrRevert() {
    const exact = options.find((o) => o === text || labelFor(o) === text)
    if (exact) onChange(exact)
    setText(exact ? labelFor(exact) : labelFor(value))
    setOpen(false)
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      if (!open) {
        setActive(initialActiveIndex())
        setOpen(true)
      } else moveActive(1)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      if (!open) {
        setActive(initialActiveIndex())
        setOpen(true)
      } else moveActive(-1)
    } else if (e.key === 'Enter') {
      if (open) {
        // 列表打开时 Enter 只属于列表：无匹配结果（或无高亮项）时吞掉按键，
        // 既不提交表单、也不把旧的已选商品当选择结果
        e.preventDefault()
        if (matches[active]) select(matches[active])
      }
    } else if (e.key === 'Escape') {
      revert()
    }
  }

  function toggleOpen(e: React.MouseEvent<HTMLButtonElement>) {
    e.preventDefault()
    if (open) revert()
    else showAll()
  }

  return (
    <div className="combobox">
      <input
        ref={inputRef}
        className="combobox-input"
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={open && matches[active] ? `${listId}-${active}` : undefined}
        value={text}
        placeholder={placeholder}
        required={required}
        readOnly={readOnly}
        onChange={(e) => {
          setText(e.target.value)
          setActive(0)
          setOpen(true)
        }}
        onFocus={() => showAll()}
        onClick={() => {
          // 选择后列表关闭但焦点仍留在输入框（选项用 mousedown+preventDefault 防失焦），
          // 再次点击不会再触发 onFocus：点输入区域也要能重新展开列表，与点箭头同效。
          if (readOnly && !open) showAll()
        }}
        onBlur={commitOrRevert}
        onKeyDown={handleKeyDown}
      />
      <button type="button" className="combobox-toggle" aria-label="展开选项列表" onMouseDown={toggleOpen}>
        ▾
      </button>
      {open && (
        <ul id={listId} role="listbox" className="combobox-list">
          {matches.length === 0 && (
            <li className="combobox-empty" role="presentation">
              无匹配项
            </li>
          )}
          {matches.map((option, i) => {
            const disabled = isDisabled?.(option) ?? false
            return (
              <li
                // 目录里有重名商品（如「抹茶吐司」），key 必须带索引避免重复
                key={`${option}-${i}`}
                id={`${listId}-${i}`}
                role="option"
                aria-selected={i === active}
                aria-disabled={disabled || undefined}
                className={`combobox-option${i === active ? ' is-active' : ''}${option === value ? ' is-selected' : ''}${disabled ? ' is-disabled' : ''}`}
                onMouseDown={(e) => {
                  e.preventDefault()
                  select(option)
                }}
                onMouseEnter={() => {
                  if (!disabled) setActive(i)
                }}
              >
                {labelFor(option)}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
