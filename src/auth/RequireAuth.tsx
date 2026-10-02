import { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from './AuthContext';
import AuthLoading from './AuthLoading';

export default function RequireAuth({ children }: { children: ReactNode }): JSX.Element {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <AuthLoading />;
  if (!user) return <Navigate to="/login" replace state={{ from: location }} />;
  // The route guard should not add an extra DOM node around the RTC layout.
  // eslint-disable-next-line react/jsx-no-useless-fragment
  return <>{children}</>;
}
