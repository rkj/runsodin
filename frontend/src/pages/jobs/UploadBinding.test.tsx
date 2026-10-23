import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Upload from './Upload'
import { models } from '../../api'

const sent: any[] = []
vi.mock('../../api', () => ({
  printFiles: { list: async () => [] },
  getApprovalSetting: async () => ({}),
  printers: { list: async () => [{ id: 42, name: 'Test U1', is_active: true }] },
  models: { schedule: vi.fn(async () => ({})), update: vi.fn() },
}))
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ role: 'admin' }) }))

class XHR {
  upload = { addEventListener: vi.fn() }
  handlers: any = {}
  status = 200
  responseText = ''
  url = ''
  addEventListener(name, cb) { this.handlers[name] = cb }
  open(_method, url) { this.url = url }
  send(body) {
    sent.push({ url: this.url, body })
    this.responseText = JSON.stringify(this.url.endsWith('import-plates')
      ? { files: [
          { id: 1, model_id: 1, project_name: 'Plate 1', target_printer_id: 42, target_printer_name: 'Test U1' },
          { id: 2, model_id: 2, project_name: 'Plate 2', target_printer_id: 42, target_printer_name: 'Test U1' },
        ] }
      : { id: 1, model_id: 1, project_name: 'Test import', target_printer_id: 42, target_printer_name: 'Test U1' })
    this.handlers.load()
  }
}
beforeEach(() => { sent.length = 0; vi.clearAllMocks(); vi.stubGlobal('XMLHttpRequest', XHR) })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
function setup() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><Upload /></MemoryRouter></QueryClientProvider>)
}

it('raw G-code requires a printer, sends target, and never schedules on import', async () => {
  const { container } = setup()
  await screen.findByRole('option', { name: 'Test U1' })
  const input = container.querySelector('input[type=file]')!
  expect(input.getAttribute('accept')).toContain('.gcode')
  fireEvent.change(input, { target: { files: [new File(['G28'], 'test.GCODE')] } })
  expect(sent).toHaveLength(0)
  fireEvent.change(screen.getByRole('combobox'), { target: { value: '42' } })
  fireEvent.change(input, { target: { files: [new File(['G28'], 'test.GCODE')] } })
  await screen.findByText('Test import')
  expect(sent[0].body.get('target_printer_id')).toBe('42')
  expect(models.schedule).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Queue for selected printer' }))
  await waitFor(() => expect(models.schedule).toHaveBeenCalledWith(1, 42))
})

it('selected 3mf imports all plate results without scheduling', async () => {
  const { container } = setup()
  await screen.findByRole('option', { name: 'Test U1' })
  fireEvent.change(screen.getByRole('combobox'), { target: { value: '42' } })
  fireEvent.change(container.querySelector('input[type=file]')!, { target: { files: [new File(['fake'], 'test.3mf')] } })
  await screen.findByText('Plate 2')
  expect(screen.getByText('Plate 1')).toBeInTheDocument()
  expect(sent[0].url).toBe('/api/print-files/import-plates')
  expect(models.schedule).not.toHaveBeenCalled()
})
