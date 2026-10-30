declare module 'claude-code' {
  interface PluginState {
    'ug-smart-router': {
      paneAgents: {
        id: string
        type: string
        description: string
        model: string
        startedAt: number
        isDone: boolean
        isError: boolean
        durationMs?: number
        tool?: string
      }[]
      paneCollapsed: boolean
      paneNow: number
    }
  }
}
