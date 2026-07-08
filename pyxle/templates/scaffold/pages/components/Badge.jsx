import React from 'react';

import styles from './Badge.module.css';

// A small presentational component that uses a CSS Module. The imported
// `styles` object maps your local class names to their build-time hashed
// versions, so styling stays scoped to this component.
export default function Badge({ children }) {
    return (
        <span className={styles.badge}>
            <span className={styles.dot} />
            {children}
        </span>
    );
}
