import AppRouter from './app/router'
import { NavigationProvider } from './app/navigation'
import { ConfirmationProvider } from './shared/ui/ConfirmationDialog'

export default function App() {
  return (
    <ConfirmationProvider>
      <NavigationProvider>
        <AppRouter />
      </NavigationProvider>
    </ConfirmationProvider>
  )
}
