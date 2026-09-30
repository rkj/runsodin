import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import PrinterModal from './PrinterModal'
import { printers } from '../../api'

vi.mock('../../api', () => ({ printers: { testConnection: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: () => false }))
afterEach(cleanup)
beforeEach(() => { vi.clearAllMocks(); vi.mocked(printers.testConnection).mockResolvedValue({ success: true, message: 'OK' }) })

function setup(api_type: string, saved = false) {
  const onSubmit = vi.fn()
  const view = render(<PrinterModal isOpen onClose={() => {}} onSubmit={onSubmit}
    printer={{ id: 17, name: 'test-printer', model: 'test', slot_count: 1, api_type, api_host: '192.0.2.1', has_api_key: saved }} />)
  const key = screen.getByPlaceholderText(api_type === 'prusalink' ? 'PrusaLink API key from Settings' : 'Moonraker API key (if required)')
  return { ...view, onSubmit, key }
}

for (const api_type of ['prusalink', 'moonraker']) {
  it(`${api_type}: sends entered key to connection test and save`, async () => {
    const { key, onSubmit, container } = setup(api_type)
    expect(key).toHaveAttribute('type', 'password')
    fireEvent.change(key, { target: { value: 'synthetic-test-key' } })
    fireEvent.click(screen.getByRole('button', { name: 'Test Connection' }))
    await waitFor(() => expect(printers.testConnection).toHaveBeenCalledWith(expect.objectContaining({ api_type, api_key: 'synthetic-test-key' })))
    fireEvent.submit(container.querySelector('form')!)
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ api_type, api_key: 'synthetic-test-key' }), 17)
    expect(onSubmit.mock.calls[0][0]).not.toHaveProperty('api_key_field')
  })

  it(`${api_type}: blank edit preserves saved credential and requests key before testing`, async () => {
    const { onSubmit, container } = setup(api_type, true)
    fireEvent.click(screen.getByRole('button', { name: 'Test Connection' }))
    expect(await screen.findByText(/Enter API key to test/)).toBeInTheDocument()
    expect(printers.testConnection).not.toHaveBeenCalled()
    fireEvent.submit(container.querySelector('form')!)
    expect(onSubmit).toHaveBeenCalledOnce()
    expect(onSubmit.mock.calls[0][0]).not.toHaveProperty('api_key')
  })
}

it('new keyless Moonraker connection remains testable', async () => {
  setup('moonraker')
  fireEvent.click(screen.getByRole('button', { name: 'Test Connection' }))
  await waitFor(() => expect(printers.testConnection).toHaveBeenCalledOnce())
  expect(vi.mocked(printers.testConnection).mock.calls[0][0]).not.toHaveProperty('api_key')
})
