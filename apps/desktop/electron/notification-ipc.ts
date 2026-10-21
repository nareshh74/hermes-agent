import { existsSync, rmSync } from 'node:fs'
import path from 'node:path'

import { BrowserWindow, ipcMain, Notification, shell } from 'electron'

import { createEventDeduper } from './event-dedupe'
import { resolveNotificationAction } from './notification-actions'
import { createLinuxNotifications } from './notification-linux'
import { createNotificationRegistry } from './notification-registry'
import type { HermesNotification } from './notification-types'

function preserveExternalWindowsShortcut(platform: NodeJS.Platform): (() => void) | null {
  if (platform !== 'win32' || !process.env.APPDATA) {
    return null
  }

  const lnk = path.join(process.env.APPDATA, 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Hermes.lnk')

  try {
    const prev = existsSync(lnk) ? shell.readShortcutLink?.(lnk) : null

    if (!prev?.target || !existsSync(prev.target)) {
      return null
    }

    if (path.normalize(prev.target).toLowerCase() === path.normalize(process.execPath).toLowerCase()) {
      return null
    }

    return () => {
      try {
        rmSync(lnk, { force: true })
        shell.writeShortcutLink?.(lnk, 'create', prev)
      } catch {
        // Ignore shortcut write races while Explorer holds the link.
      }
    }
  } catch {
    return null
  }
}

interface NotificationHost {
  getMainWindow: () => BrowserWindow | null
  focusWindow: (window: BrowserWindow) => void
  platform?: NodeJS.Platform
}

export function registerNativeNotifications({
  getMainWindow,
  focusWindow,
  platform = process.platform
}: NotificationHost): { dispose: () => void } {
  const dedupeIntervalMs = 1000
  const isDuplicateNotification = createEventDeduper(dedupeIntervalMs)
  const deliveries = new Map<string, Promise<boolean>>()
  const linux = platform === 'linux' ? createLinuxNotifications() : undefined
  const notifications = createNotificationRegistry({ releaseOnClose: Boolean(linux) })

  ipcMain.handle('hermes:notify', async (event, payload: HermesNotification) => {
    // The source renderer owns runtime bindings and plugin callbacks.
    const sourceWindow = BrowserWindow.fromWebContents(event.sender)
    const targetWindow = () => (sourceWindow && !sourceWindow.isDestroyed() ? sourceWindow : getMainWindow())

    if (!linux && !Notification.isSupported()) {
      return false
    }

    // Peer renderers share one OS notification for the same event.
    const key = `${payload?.kind ?? ''}:${payload?.sessionId ?? payload?.tag ?? ''}`

    if (isDuplicateNotification(key)) {
      return deliveries.get(key) ?? false
    }

    const actions = Array.isArray(payload?.actions) ? payload.actions : []
    const icon = typeof payload?.icon === 'string' && payload.icon.trim() ? payload.icon.trim() : undefined

    const options = {
      title: payload?.title || 'Hermes',
      body: payload?.body || '',
      silent: Boolean(payload?.silent),
      ...(icon ? { icon } : {}),
      actions: actions.map(action => ({ type: 'button' as const, text: String(action?.text || '') }))
    }

    const restoreShortcut = preserveExternalWindowsShortcut(platform)
    const notification = linux ? linux.create(options) : new Notification(options)

    notification.on('click', () => {
      const window = targetWindow()

      if (!window || window.isDestroyed()) {
        return
      }

      focusWindow(window)

      const focusSessionId = payload?.focusSessionId || payload?.sessionId

      if (focusSessionId) {
        window.webContents.send('hermes:focus-session', focusSessionId)
      }

      if (payload?.activate || payload?.notifyId) {
        window.webContents.send('hermes:notification-activate', {
          activate: payload?.activate,
          notifyId: window === sourceWindow ? payload?.notifyId : undefined,
          tag: payload?.tag
        })
      }
    })
    notification.on('action', (actionEvent, index) => {
      const window = targetWindow()

      if (!window || window.isDestroyed()) {
        return
      }

      const action = resolveNotificationAction(actions, actionEvent, index)

      if (!action?.id) {
        return
      }

      if (payload?.sessionId && !payload?.notifyId && !payload?.activate) {
        // Runtime actions belong to the source renderer, never the fallback.
        if (window !== sourceWindow) {
          return
        }

        window.webContents.send('hermes:notification-action', { sessionId: payload.sessionId, actionId: action.id })

        return
      }

      focusWindow(window)
      window.webContents.send('hermes:notification-activate', {
        actionId: action.id,
        activate: action.activate || payload?.activate,
        notifyId: window === sourceWindow ? payload?.notifyId : undefined,
        tag: payload?.tag
      })
    })
    notifications.retain(notification)

    // Peers share the actual outcome, including pending/failed Linux delivery.
    const delivery = Promise.resolve(notification.show())
      .then(result => result !== false)
      .finally(() => restoreShortcut?.())

    deliveries.set(key, delivery)
    setTimeout(() => {
      if (deliveries.get(key) === delivery) {
        deliveries.delete(key)
      }
    }, dedupeIntervalMs).unref()

    return delivery
  })

  return { dispose: () => linux?.dispose() }
}
