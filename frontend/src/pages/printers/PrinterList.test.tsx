import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Printers from './Printers'
import { MemoryRouter } from 'react-router-dom'
vi.mock('../../permissions', () => ({ canDo: () => true, clearSensitiveBrowserState: vi.fn() }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: false, max_printers: 5 }) }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: null }) }))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
const sample = { id: 1, name: 'Synthetic U1', model: 'U1', nickname: null, api_type: 'moonraker', api_host: '192.0.2.1', has_api_key: true, is_active: true, tags: [], filament_slots: [1,2,3,4].map(i => ({id:i,slot_number:i,filament_type:'empty', color:null,color_hex:null})), last_seen: '2026-09-29T00:00:00', bed_temp: 25, nozzle_temp: 26, bed_target_temp: 0, nozzle_target_temp:0, gcode_state: 'IDLE', print_stage:'Idle' }
function setup(responses: Record<string, unknown>) {
 vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ok:true,status:200,json:async()=> responses[url] ?? []})))
 const queryClient = new QueryClient({ defaultOptions: { queries: { retry:false } } })
 return render(<QueryClientProvider client={queryClient}><MemoryRouter><Printers/></MemoryRouter></QueryClientProvider>)
}
it('renders Moonraker and Prusa cards with real API array contracts', async () => {
 setup({'/api/printers?active_only=false':[sample,{...sample,id:2,name:'Synthetic Prusa',api_type:'prusalink',filament_slots:sample.filament_slots.slice(0,1)}]})
 expect(await screen.findByText('Synthetic U1')).toBeInTheDocument()
 expect(await screen.findByText('Synthetic Prusa')).toBeInTheDocument()
 expect(screen.getAllByTestId('printer-card')).toHaveLength(2)
})

it('shows a load error and retries successfully instead of hiding the failure', async () => {
 let failed = true
 vi.stubGlobal('fetch', vi.fn(async (url: string) => {
   if (url.startsWith('/api/printers?')) {
     if (failed) return { ok: false, status: 500, json: async () => ({}) }
     return { ok: true, status: 200, json: async () => [sample] }
   }
   return { ok: true, status: 200, json: async () => [] }
 }))
 const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
 render(<QueryClientProvider client={queryClient}><MemoryRouter><Printers/></MemoryRouter></QueryClientProvider>)
 expect(await screen.findByRole('alert')).toHaveTextContent('Unable to load printers. Please try again.')
 expect(screen.queryByText('No printers configured yet.')).not.toBeInTheDocument()
 failed = false
 fireEvent.click(screen.getByRole('button', {name: 'Retry'}))
 expect(await screen.findByText('Synthetic U1')).toBeInTheDocument()
 expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
