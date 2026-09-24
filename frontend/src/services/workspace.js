export function validTargets(machines, selections) {
  return machines.filter((machine) => machine.enabled).map((machine) => ({
    machine_id: machine.machine_id,
    browsers: [...new Set(Array.isArray(selections[machine.machine_id]) ? selections[machine.machine_id] : [])]
      .filter((browser) => machine.capabilities?.browsers?.some((item) => item.name === browser)),
  })).filter((target) => target.browsers.length)
}

export function parseTableItems(rows) {
  const items = []
  const usedIds = new Set()
  for (const [index, source] of rows.entries()) {
    const item = { id: source.id.trim(), name: source.name.trim(), url: source.url.trim() }
    if (!item.id && !item.name && !item.url) continue
    if (!item.id || !item.name || !item.url) throw new Error(`第 ${index + 1} 行的 ID、名称和 URL 必须全部填写`)
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(item.id)) throw new Error(`第 ${index + 1} 行 ID 必须以字母或数字开头，只允许字母、数字、点、下划线和连字符，最多 64 字符`)
    if (usedIds.has(item.id)) throw new Error(`第 ${index + 1} 行的 ID 与前面重复：${item.id}`)
    if (item.name.length > 80 || /[\r\n\t]/.test(item.name)) throw new Error(`第 ${index + 1} 行名称最长 80 字符，不能包含换行或制表符`)
    try {
      const parsed = new URL(item.url)
      if (!/^https?:\/\//i.test(item.url) || !['http:', 'https:'].includes(parsed.protocol)
        || parsed.username || parsed.password || item.url.length > 2048 || /[\r\n\t]/.test(item.url)) throw new Error()
    } catch { throw new Error(`第 ${index + 1} 行 URL 必须是完整 HTTP/HTTPS 地址，不能包含用户名或密码，最长 2048 字符`) }
    usedIds.add(item.id)
    items.push(item)
  }
  return items
}
